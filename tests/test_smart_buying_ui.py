import json
import re
import shutil
import subprocess
from html import unescape

import pytest

from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page, render_layout

CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@pytest.mark.skipif(not CHROME, reason="Chrome is required")
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_buying_modal_submits_once_with_cash_account_price_and_urgency(lang, tmp_path):
    prelude = """
    <script>
    window.errors = []; window.posts = []; window.parents = []; window.polls = [];
    window.release = null;
    addEventListener('error', e => errors.push(e.message));
    addEventListener('unhandledrejection', e => errors.push(String(e.reason)));
    window.setInterval = cb => { polls.push(cb); return polls.length; };
    window.fetch = async (url, options = {}) => {
      if (url === '/api/accounts') return {ok: true, json: async () => ({accounts: [
        {account_id:'A', cash_by_currency:{USD:10000}}
      ]})};
      if (url === '/api/market-data') return {ok:true,json:async()=>({rows:{}})};
      if (options.method !== 'POST') return {ok:true,json:async()=>({ok:true, parents:url.includes('buying')?parents:[]})};
      posts.push({url, payload:options.body ? JSON.parse(options.body):null});
      if (url.endsWith('/stop')) { parents[0].state='STOPPING'; return {ok:true,json:async()=>({ok:true})}; }
      await new Promise(resolve => { release = resolve; });
      parents=[{parent_id:'buy/one',account_ids:['A'],symbol:'AAPL',quantity:'500',filled:'0',remaining:'500',
        reserved:'100',state:'RUNNING',active:true,start:new Date().toISOString(),reason:'waiting for volume'}];
      return {ok:true,json:async()=>({ok:true,parent:parents[0]})};
    };
    </script>
    """
    checks = """
    <pre id='test-result'>pending</pre><script>
    (async()=>{
      const el=id=>document.getElementById(id);
      const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
      const check=(ok,msg)=>{if(!ok)errors.push(msg);};
      try {
        await settle();
        document.querySelector("label[for='algo-mode-h']").click();
        el('algo_symbol').value='AAPL';
        el('algo-form').dispatchEvent(new Event('submit',{cancelable:true}));
        await settle();
        check(!el('smart-buying-modal').hidden,'H opens its own dialog');
        check(el('smart-selling-modal').hidden,'G stays closed');
        el('smart-buying-confirm').click();
        check(posts.length===0,'Account selection required');
        document.querySelector('[data-smart-buying-account-id="A"]').checked=true;
        el('smart-buying-mode').value='urgent';
        el('smart-buying-maximum-price').value='25.5';
        document.querySelector("label[for='smart-buying-no-deadline']").click();
        el('smart-buying-confirm').click(); el('smart-buying-confirm').click();
        check(posts.length===1,'Only one start request');
        check(posts[0].url==='/api/smart-buying','Dedicated buying endpoint');
        check(JSON.stringify(posts[0].payload)===JSON.stringify({account_ids:['A'],symbol:'AAPL',mode:'urgent',
          start_time:null,deadline:null,maximum_price:25.5}),'Correct buying settings');
        release(); await settle();
        check(el('smart-buying-modal').hidden,'Success closes the buying dialog');
        check(el('smart-buying-status-table').rows.length===2,'Buying status visible');
        el('smart-buying-status-table').querySelector('button').click(); await settle();
        check(posts[1].url==='/api/smart-buying/buy%2Fone/stop','Buying stop targets the right parent');
        check(el('smart-status-table').hidden,'Buying status does not populate G');
      } catch(e) {errors.push(e.stack||String(e));}
      el('test-result').textContent=JSON.stringify(errors);
    })();</script>
    """
    page = render_layout(lang, "control-panel", render_control_panel_page(
        lang, {"A": AccountMeta("A", 1, "Tiger", "API")}, ["AAPL"],
    ))
    path = tmp_path / "buying.html"
    path.write_text(page.replace("<head>", "<head>" + prelude).replace("</body>", checks + "</body>"))
    result = subprocess.run([
        CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
        "--user-data-dir=" + str(tmp_path / "profile"), "--virtual-time-budget=1000", "--dump-dom", path.as_uri(),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    found = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, re.S)
    assert found is not None
    assert json.loads(unescape(found.group(1))) == []
