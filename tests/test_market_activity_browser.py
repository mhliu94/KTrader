"""Real Chrome checks; all requests are intercepted with synthetic data."""
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from trading_ui.templates import render_layout, render_market_data_page

CHROME = shutil.which("google-chrome") or shutil.which("chromium")


def sample_page(lang="en", *, checks=True, symbols=None):
    prelude = """
    <script>
      window.errors=[]; window.calls=[]; window.deferA=false; window.releaseA=null; window.failStatus=null;
      window.setInterval=callback=>{window.pollMarket=callback;return 1;};
      window.clearInterval=()=>{window.pollStopped=true;};
      addEventListener('error',e=>errors.push(e.message));
      addEventListener('unhandledrejection',e=>errors.push(String(e.reason)));
      window.sample=symbol=>({
        ok:true,symbol,timezone:'America/New_York',generated_at:1794237720,
        quote_state:'live',order_feed:'live',last_trade:{time:1794237719,quantity:25,price:symbol==='XYZ'?12.3456:45.6789},
        windows:Array.from({length:4},(_,i)=>({
          start:1794237600-i*300,end:1794237900-i*300,current:i===0,
          market_volume:1000+i*100,bought:100+i,sold:25,gross:125+i,
          vwap_estimate:12.32+i*.01,market_complete:i!==3,our_complete:true
        }))
      });
      window.fetch=async(url,options)=>{
        calls.push(url);
        const symbol=new URL(url,'http://test.invalid').searchParams.get('symbol');
        if(failStatus)return {ok:false,status:failStatus};
        if(symbol==='XYZ'&&deferA)return new Promise(resolve=>releaseA=()=>resolve({ok:true,json:async()=>sample('XYZ')}));
        return {ok:true,json:async()=>sample(symbol)};
      };
    </script>
    """
    assertions = """
    <pre id="test-result">pending</pre><script>
    (async()=>{
      const el=id=>document.getElementById(id);
      const check=(condition,label)=>{if(!condition)errors.push(label);};
      const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
      const choose=symbol=>{el('md-symbol').value=symbol;el('md-symbol').dispatchEvent(new Event('change'));};
      try {
        await settle();
        check(el('md-window-rows').rows.length===4,'four windows');
        check(el('md-window-rows').rows[0].cells.length===6,'six columns');
        check(el('md-window-rows').rows[0].cells[2].textContent==='100','bought');
        check(el('md-window-rows').rows[0].cells[3].textContent==='25','sold');
        check(el('md-window-rows').rows[0].cells[4].textContent==='125','gross');
        check(el('md-trade-quantity').textContent==='25','last quantity');
        check(el('md-trade-price').textContent.includes('12.3456'),'last price');
        check(el('md-trade-time').textContent!=='—','last trade time');
        check(!el('md-windows').contains(el('md-trade-price')),'last trade outside table');
        check(el('md-window-rows').rows[3].cells[1].textContent.endsWith('*'),'partial coverage');
        check(!el('md-window-rows').rows[0].cells[1].textContent.endsWith('*'),'complete coverage');
        check(calls.every(url=>url.startsWith('/api/market-data?symbol=')),'protected API only');
        choose('ABC'); await settle();
        check(el('md-trade-price').textContent.includes('45.6789'),'symbol switch');
        deferA=true; choose('XYZ'); await settle();
        check(el('md-trade-price').textContent==='—','switch clears old price immediately');
        choose('ABC'); await settle(); releaseA(); await settle();
        check(el('md-trade-price').textContent.includes('45.6789'),'late old response is ignored');
        failStatus=503; await pollMarket(); await settle();
        check(el('md-trade-price').textContent==='—','failed request clears stale price');
        check(el('md-error').textContent.length>0,'failed request visible');
        failStatus=null; await pollMarket(); await settle();
        check(el('md-window-rows').rows.length===4,'poll recovers');
        failStatus=403; await pollMarket(); await settle();
        check(window.pollStopped,'forbidden stops polling');
        check(el('md-window-rows').rows.length===1,'forbidden clears data');
      }catch(error){errors.push(error.stack||String(error));}
      el('test-result').textContent=JSON.stringify(errors);
    })();
    </script>
    """
    page = render_layout(lang, "market-data", render_market_data_page(lang, symbols if symbols is not None else ["XYZ", "ABC"]),
                         current_user="lmh", can_access_admin_control=True, can_manage_trading=True)
    return page.replace("<head>", "<head>"+prelude).replace("</body>", (assertions if checks else "")+"</body>")


@pytest.mark.skipif(not CHROME, reason="Chrome is required")
@pytest.mark.parametrize("lang,width", [("en", 1440), ("zh", 390)])
def test_windows_symbol_switch_stale_responses_and_auth_failure(lang, width, tmp_path):
    path=tmp_path/"market.html"
    path.write_text(sample_page(lang))
    result=subprocess.run([
        CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
        "--user-data-dir="+str(tmp_path/"profile"), "--window-size="+str(width)+",1000",
        "--virtual-time-budget=1500", "--dump-dom", path.as_uri(),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    found=re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, re.S)
    assert found is not None
    assert json.loads(unescape(found.group(1))) == []


def test_symbol_options_are_escaped_and_empty_configuration_is_explicit():
    page=render_market_data_page("en", ['"><img src=x onerror=alert(1)>'])
    assert '<img src=x' not in page
    assert '&lt;img' in page
    empty=render_market_data_page("en", [])
    assert "No symbols configured." in empty
    assert re.search(r'<select id="md-symbol"[^>]*disabled',empty)
