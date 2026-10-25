-- Freqtrade dry-run trades database (tradesv3.dryrun.sqlite) of the reconciliation scenario, as SQL.
-- Tables and columns follow freqtrade/persistence/trade_model.py at commit 1c0d709401b5 (SQLAlchemy on SQLite:
-- DateTime as naive UTC text, Boolean as 0/1, Float as REAL). Dry-run order ids follow
-- Exchange.create_dry_run_order: dry_run_<side>_<pair>_<uuid4>. Build the database with
--   python -c "import sqlite3,sys; sqlite3.connect(sys.argv[2]).executescript(open(sys.argv[1]).read())" \
--     tradesv3.dryrun.sql tradesv3.dryrun.sqlite
-- Trade 2 of 10:15 (ETH/USDT) was deleted by the bot after its entry order timed out unfilled (see freqtrade.log);
-- SQLite reused id 2 for the next trade. The bot ran with stoploss_on_exchange: every trade has a stop-loss order
-- (Exchange.create_stoploss in dry-run: a dry_run_ order of type stop_loss_limit), cancelled unfilled when the trade
-- exits (no ft_cancel_reason), except trade 2's, which triggered. Dry-run limit orders fill all at once
-- (check_dry_limit_order_filled), so no order here is partially filled.
CREATE TABLE trades (
    id INTEGER NOT NULL,
    exchange VARCHAR(25) NOT NULL,
    pair VARCHAR(25) NOT NULL,
    base_currency VARCHAR(25),
    stake_currency VARCHAR(25),
    is_open BOOLEAN NOT NULL,
    fee_open FLOAT NOT NULL,
    fee_open_cost FLOAT,
    fee_open_currency VARCHAR(25),
    fee_close FLOAT NOT NULL,
    fee_close_cost FLOAT,
    fee_close_currency VARCHAR(25),
    open_rate FLOAT NOT NULL,
    open_rate_requested FLOAT,
    open_trade_value FLOAT,
    close_rate FLOAT,
    close_rate_requested FLOAT,
    realized_profit FLOAT,
    close_profit FLOAT,
    close_profit_abs FLOAT,
    stake_amount FLOAT NOT NULL,
    max_stake_amount FLOAT,
    amount FLOAT NOT NULL,
    amount_requested FLOAT,
    open_date DATETIME NOT NULL,
    close_date DATETIME,
    stop_loss FLOAT,
    stop_loss_pct FLOAT,
    initial_stop_loss FLOAT,
    initial_stop_loss_pct FLOAT,
    is_stop_loss_trailing BOOLEAN NOT NULL,
    max_rate FLOAT,
    min_rate FLOAT,
    exit_reason VARCHAR(255),
    exit_order_status VARCHAR(100),
    strategy VARCHAR(100),
    enter_tag VARCHAR(255),
    timeframe INTEGER,
    trading_mode VARCHAR(7),
    amount_precision FLOAT,
    price_precision FLOAT,
    precision_mode INTEGER,
    precision_mode_price INTEGER,
    contract_size FLOAT,
    leverage FLOAT,
    is_short BOOLEAN NOT NULL,
    liquidation_price FLOAT,
    interest_rate FLOAT NOT NULL,
    funding_fees FLOAT,
    funding_fee_running FLOAT,
    record_version INTEGER NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE orders (
    id INTEGER NOT NULL,
    ft_trade_id INTEGER NOT NULL,
    ft_order_side VARCHAR(25) NOT NULL,
    ft_pair VARCHAR(25) NOT NULL,
    ft_is_open BOOLEAN NOT NULL,
    ft_amount FLOAT NOT NULL,
    ft_price FLOAT NOT NULL,
    ft_cancel_reason VARCHAR(255),
    order_id VARCHAR(255) NOT NULL,
    status VARCHAR(255),
    symbol VARCHAR(25),
    order_type VARCHAR(50),
    side VARCHAR(25),
    price FLOAT,
    average FLOAT,
    amount FLOAT,
    filled FLOAT,
    remaining FLOAT,
    cost FLOAT,
    stop_price FLOAT,
    order_date DATETIME,
    order_filled_date DATETIME,
    order_update_date DATETIME,
    funding_fee FLOAT,
    ft_fee_base FLOAT,
    ft_order_tag VARCHAR(255),
    PRIMARY KEY (id),
    CONSTRAINT _order_pair_order_id UNIQUE (ft_pair, order_id),
    FOREIGN KEY(ft_trade_id) REFERENCES trades (id)
);
INSERT INTO trades VALUES (1,'binance','BTC/USDT','BTC','USDT',0,0.001,0.0976,'USDT',0.001,0.098576,'USDT',61000.0,61000.0,97.6976,61610.0,61610.0,0.779824,0.00798,0.779824,97.6,97.6,0.0016,0.0016,'2024-03-01 10:05:02.481000','2024-03-01 10:41:30.112000',60390.0,-0.01,60390.0,-0.01,0,61655.0,60970.0,'roi','closed','SampleStrategy','rsi_cross',5,'SPOT',1e-05,0.01,4,4,1.0,1.0,0,NULL,0.0,NULL,NULL,2);
INSERT INTO trades VALUES (2,'binance','BTC/USDT','BTC','USDT',0,0.001,0.09792,'USDT',0.001,0.096912,'USDT',61200.0,61200.0,98.01792,60570.0,60588.0,-1.202832,-0.01227,-1.202832,97.92,97.92,0.0016,0.0016,'2024-03-01 11:30:02.904000','2024-03-01 12:13:20.448000',60588.0,-0.01,60588.0,-0.01,0,61330.0,60560.0,'stoploss_on_exchange','closed','SampleStrategy','bb_lower',5,'SPOT',1e-05,0.01,4,4,1.0,1.0,0,NULL,0.0,NULL,NULL,2);
INSERT INTO trades VALUES (3,'binance','ETH/USDT','ETH','USDT',0,0.001,0.10215,'USDT',0.001,0.1026,'USDT',3405.0,3405.0,102.25215,3420.0,3420.0,0.24525,0.0024,0.24525,102.15,102.15,0.03,0.03,'2024-03-01 12:35:03.118000','2024-03-01 12:58:41.009000',3370.95,-0.01,3370.95,-0.01,0,3424.0,3401.5,'exit_signal','closed','SampleStrategy','rsi_cross',5,'SPOT',0.0001,0.01,4,4,1.0,1.0,0,NULL,0.0,NULL,NULL,2);
INSERT INTO trades VALUES (4,'binance','BTC/USDT','BTC','USDT',0,0.001,0.0984,'USDT',0.001,0.098864,'USDT',61500.0,61500.0,98.4984,61790.0,61800.0,0.266736,0.00271,0.266736,98.4,98.4,0.0016,0.0016,'2024-03-01 13:00:03.377000','2024-03-01 13:52:00.775000',60885.0,-0.01,60885.0,-0.01,0,61812.0,61481.0,'roi','closed','SampleStrategy','bb_lower',5,'SPOT',1e-05,0.01,4,4,1.0,1.0,0,NULL,0.0,NULL,NULL,2);
INSERT INTO orders VALUES (1,1,'buy','BTC/USDT',0,0.0016,61000.0,NULL,'dry_run_buy_BTC/USDT_6f1c0c5e-2b7d-4a51-9c3e-0d8b1f4e7a21','closed','BTC/USDT','limit','buy',61000.0,61000.0,0.0016,0.0016,0.0,97.6,NULL,'2024-03-01 10:05:02.481000','2024-03-01 10:05:02.633000','2024-03-01 10:05:02.633000',0.0,NULL,'rsi_cross');
INSERT INTO orders VALUES (2,1,'sell','BTC/USDT',0,0.0016,61610.0,NULL,'dry_run_sell_BTC/USDT_9a0e5d14-7c3f-4f0b-8e61-52b9c6d0a3f4','closed','BTC/USDT','limit','sell',61610.0,61610.0,0.0016,0.0016,0.0,98.576,NULL,'2024-03-01 10:41:07.250000','2024-03-01 10:41:30.112000','2024-03-01 10:41:30.112000',0.0,NULL,'roi');
INSERT INTO orders VALUES (4,2,'buy','BTC/USDT',0,0.0016,61200.0,NULL,'dry_run_buy_BTC/USDT_c3d9e2a7-41b8-4c6e-a2f5-7e0d93b1c845','closed','BTC/USDT','limit','buy',61200.0,61200.0,0.0016,0.0016,0.0,97.92,NULL,'2024-03-01 11:30:02.904000','2024-03-01 11:31:40.517000','2024-03-01 11:31:40.517000',0.0,NULL,'bb_lower');
INSERT INTO orders VALUES (5,2,'stoploss','BTC/USDT',0,0.0016,60588.0,NULL,'dry_run_sell_BTC/USDT_0b7f3e91-d5a2-4e8c-b6f0-3a9c2d71e5b8','closed','BTC/USDT','stop_loss_limit','sell',59982.12,60570.0,0.0016,0.0016,0.0,96.912,60588.0,'2024-03-01 11:31:41.002000','2024-03-01 12:13:20.448000','2024-03-01 12:13:20.448000',0.0,NULL,'stoploss');
INSERT INTO orders VALUES (6,3,'buy','ETH/USDT',0,0.03,3405.0,NULL,'dry_run_buy_ETH/USDT_5e2a8c47-9f1d-4b3a-8d27-c0e6f4a19b52','closed','ETH/USDT','limit','buy',3405.0,3405.0,0.03,0.03,0.0,102.15,NULL,'2024-03-01 12:35:03.118000','2024-03-01 12:35:03.270000','2024-03-01 12:35:03.270000',0.0,NULL,'rsi_cross');
INSERT INTO orders VALUES (7,3,'sell','ETH/USDT',0,0.03,3420.0,NULL,'dry_run_sell_ETH/USDT_a81f4d3c-62e9-4b70-95c8-1d7e0b2f6a93','closed','ETH/USDT','limit','sell',3420.0,3420.0,0.03,0.03,0.0,102.6,NULL,'2024-03-01 12:58:02.700000','2024-03-01 12:58:41.009000','2024-03-01 12:58:41.009000',0.0,NULL,'exit_signal');
INSERT INTO orders VALUES (8,4,'buy','BTC/USDT',0,0.0016,61500.0,NULL,'dry_run_buy_BTC/USDT_e47b2c90-3d1f-4a85-b9e6-8f2a0c5d7b13','closed','BTC/USDT','limit','buy',61500.0,61500.0,0.0016,0.0016,0.0,98.4,NULL,'2024-03-01 13:00:03.377000','2024-03-01 13:06:12.880000','2024-03-01 13:06:12.880000',0.0,NULL,'bb_lower');
INSERT INTO orders VALUES (9,4,'sell','BTC/USDT',0,0.0016,61800.0,'cancelled due to timeout','dry_run_sell_BTC/USDT_1c6e9a2f-8b4d-4f37-a0e5-6d2b9c8f1e40','canceled','BTC/USDT','limit','sell',61800.0,61800.0,0.0016,0.0,0.0016,0.0,NULL,'2024-03-01 13:40:05.640000',NULL,'2024-03-01 13:50:06.220000',0.0,NULL,'roi');
INSERT INTO orders VALUES (10,4,'sell','BTC/USDT',0,0.0016,61790.0,NULL,'dry_run_sell_BTC/USDT_7d3b0f8e-c2a6-4e19-b5d4-90f1a7e2c6b8','closed','BTC/USDT','limit','sell',61790.0,61790.0,0.0016,0.0016,0.0,98.864,NULL,'2024-03-01 13:50:08.031000','2024-03-01 13:52:00.775000','2024-03-01 13:52:00.775000',0.0,NULL,'roi');
INSERT INTO orders VALUES (11,1,'stoploss','BTC/USDT',0,0.0016,60390.0,NULL,'dry_run_sell_BTC/USDT_2e8f4b61-0a9c-4d73-b5e2-c71d9f30a846','canceled','BTC/USDT','stop_loss_limit','sell',59786.1,NULL,0.0016,0.0,0.0016,0.0,60390.0,'2024-03-01 10:05:03.104000',NULL,'2024-03-01 10:41:07.198000',0.0,NULL,'stoploss');
INSERT INTO orders VALUES (12,3,'stoploss','ETH/USDT',0,0.03,3370.95,NULL,'dry_run_sell_ETH/USDT_9c4a7e20-5b13-4f8d-a6c9-03e2b7d51f94','canceled','ETH/USDT','stop_loss_limit','sell',3337.24,NULL,0.03,0.0,0.03,0.0,3370.95,'2024-03-01 12:35:03.702000',NULL,'2024-03-01 12:58:02.651000',0.0,NULL,'stoploss');
INSERT INTO orders VALUES (13,4,'stoploss','BTC/USDT',0,0.0016,60885.0,NULL,'dry_run_sell_BTC/USDT_f05b3d92-7e61-4a2c-8d4f-b19c6e07a253','canceled','BTC/USDT','stop_loss_limit','sell',60276.15,NULL,0.0016,0.0,0.0016,0.0,60885.0,'2024-03-01 13:06:13.302000',NULL,'2024-03-01 13:40:05.580000',0.0,NULL,'stoploss');
