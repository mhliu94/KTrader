from futu import *
import logging
import os
import sys


HOST = "127.0.0.1"
PORT = 11111   # default OpenD port
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOGGER = logging.getLogger("futu-account-query")


def configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )


def log_text(value):
    text = str(value)
    if not text:
        return
    for line in text.splitlines():
        LOGGER.info("%s", line)


def print_account_list(trd_ctx):
    ret, data = trd_ctx.get_acc_list()
    if ret != RET_OK:
        LOGGER.error("Failed to get account list:")
        log_text(data)
        return None

    LOGGER.info("=== Account List ===")
    log_text(data)

    return data


def choose_account(acc_df, trd_env=TrdEnv.REAL):
    """
    Pick the first account matching the requested trading environment.
    """
    if acc_df is None or len(acc_df) == 0:
        return None

    matches = acc_df[acc_df["trd_env"] == trd_env]
    if len(matches) == 0:
        return None

    row = matches.iloc[0]
    return {
        "acc_id": int(row["acc_id"]),
        "trd_env": row["trd_env"],
        "trd_market_auth": row.get("trdmarket_auth", None),
    }


def query_funds(trd_ctx, acc_id, trd_env):
    LOGGER.info("=== Account Funds / Cash ===")
    ret, data = trd_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
    if ret != RET_OK:
        LOGGER.error("Failed to query account funds:")
        log_text(data)
        return

    log_text(data)

    # Print a few common fields if present
    if len(data) > 0:
        row = data.iloc[0]
        for field in [
            "power",
            "cash",
            "market_val",
            "total_assets",
            "avl_withdrawal_cash",
            "max_power_short",
            "net_cash_power",
        ]:
            if field in row.index:
                LOGGER.info("%s: %s", field, row[field])


def query_positions(trd_ctx, acc_id, trd_env):
    LOGGER.info("=== Positions ===")
    ret, data = trd_ctx.position_list_query(
        trd_env=trd_env,
        acc_id=acc_id
    )
    if ret != RET_OK:
        LOGGER.error("Failed to query positions:")
        log_text(data)
        return

    if len(data) == 0:
        LOGGER.info("No positions found.")
        return

    log_text(data)

    # Optional: print selected fields if present
    preferred_cols = [
        "code",
        "stock_name",
        "qty",
        "can_sell_qty",
        "cost_price",
        "nominal_price",
        "pl_val",
        "pl_ratio",
        "market_val",
    ]
    cols = [c for c in preferred_cols if c in data.columns]
    if cols:
        LOGGER.info("=== Position Summary ===")
        log_text(data[cols].to_string(index=False))


def main():
    configure_logging()
    trd_ctx = OpenSecTradeContext(host=HOST, port=PORT)

    try:
        acc_df = print_account_list(trd_ctx)
        acct = choose_account(acc_df, trd_env=TrdEnv.REAL)

        if acct is None:
            LOGGER.info("No REAL trading account found. Trying SIMULATE...")
            acct = choose_account(acc_df, trd_env=TrdEnv.SIMULATE)

        if acct is None:
            LOGGER.error("No usable account found.")
            sys.exit(1)

        acc_id = acct["acc_id"]
        trd_env = acct["trd_env"]

        LOGGER.info("Using account acc_id=%s, trd_env=%s", acc_id, trd_env)

        query_funds(trd_ctx, acc_id, trd_env)
        query_positions(trd_ctx, acc_id, trd_env)

    finally:
        trd_ctx.close()


if __name__ == "__main__":
    main()
