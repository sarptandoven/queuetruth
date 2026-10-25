-- Freqtrade live trades database (tradesv3.sqlite) of the reconciliation scenario, as SQL: the bot of
-- tradesv3.dryrun.sql run live on Binance spot over the same four hours. Only the columns the importer reads, plus
-- the trade totals (freqtrade/persistence/trade_model.py at commit 1c0d709401b5; SQLAlchemy on SQLite: DateTime as
-- naive UTC text, Boolean as 0/1, Float as REAL). Order ids are Binance's numeric order ids, so order_date is the
-- exchange's order time (ccxt `timestamp`) and order_filled_date its last trade time (ccxt `lastTradeTimestamp`).
-- What the exchange did differently from the backtest:
--   trade 1 (BTC 10:05): entry and ROI exit fill inside the backtest's candles; the stop-loss never triggers.
--   trade 2 (ETH 10:15): the entry fills 0.012 of 0.03 before its 10-minute timeout; the exit_signal exit is a
--     market order that sells 0.012 at 3424.1.
--   trade 3 (BTC 11:30): the stop-loss (stop_loss_limit, stop 60588) triggers and fills at 60560.
--   trade 4 (ETH 12:00): the ROI exit at 3423.9 times out unfilled and is replaced at 3421.
--   trade 5 (BTC 13:00): the entry fills at 13:08:41, before the backtest's fill candle (13:10).
-- stoploss_on_exchange: a stop-loss order follows each entry fill and is cancelled (no ft_cancel_reason) when the
-- trade exits.
CREATE TABLE trades (
    id INTEGER NOT NULL, exchange VARCHAR(25) NOT NULL, pair VARCHAR(25) NOT NULL, is_open BOOLEAN NOT NULL,
    open_date DATETIME NOT NULL, close_date DATETIME, amount FLOAT NOT NULL, open_rate FLOAT NOT NULL,
    close_rate FLOAT, close_profit_abs FLOAT, fee_open_cost FLOAT, fee_open_currency VARCHAR(25),
    fee_close_cost FLOAT, fee_close_currency VARCHAR(25), enter_tag VARCHAR(255), exit_reason VARCHAR(255),
    timeframe INTEGER, trading_mode VARCHAR(7), is_short BOOLEAN NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE orders (
    id INTEGER NOT NULL, ft_trade_id INTEGER NOT NULL, ft_order_side VARCHAR(25) NOT NULL, ft_pair VARCHAR(25) NOT NULL,
    ft_is_open BOOLEAN NOT NULL, ft_amount FLOAT NOT NULL, ft_price FLOAT NOT NULL, ft_cancel_reason VARCHAR(255),
    order_id VARCHAR(255) NOT NULL, status VARCHAR(255), symbol VARCHAR(25), order_type VARCHAR(50), side VARCHAR(25),
    price FLOAT, average FLOAT, amount FLOAT, filled FLOAT, remaining FLOAT, cost FLOAT, stop_price FLOAT,
    order_date DATETIME, order_filled_date DATETIME, order_update_date DATETIME, ft_fee_base FLOAT,
    ft_order_tag VARCHAR(255), PRIMARY KEY (id)
);
INSERT INTO trades VALUES (1,'binance','BTC/USDT',0,'2024-03-01 10:05:02.512000','2024-03-01 10:41:52.660000',0.0016,61000.0,61610.0,0.779824,0.0976,'USDT',0.098576,'USDT','rsi_cross','roi',5,'SPOT',0);
INSERT INTO trades VALUES (2,'binance','ETH/USDT',0,'2024-03-01 10:15:03.377000','2024-03-01 11:00:02.461000',0.012,3400.0,3424.1,0.2073108,0.0408,'USDT',0.0410892,'USDT','rsi_cross','exit_signal',5,'SPOT',0);
INSERT INTO trades VALUES (3,'binance','BTC/USDT',0,'2024-03-01 11:30:02.951000','2024-03-01 12:13:20.905000',0.0016,61200.0,60560.0,-1.218816,0.09792,'USDT',0.096896,'USDT','bb_lower','stoploss_on_exchange',5,'SPOT',0);
INSERT INTO trades VALUES (4,'binance','ETH/USDT',0,'2024-03-01 12:00:02.630000','2024-03-01 12:36:10.441000',0.03,3390.0,3421.0,0.72567,0.1017,'USDT',0.10263,'USDT','rsi_cross','roi',5,'SPOT',0);
INSERT INTO trades VALUES (5,'binance','BTC/USDT',0,'2024-03-01 13:00:03.402000','2024-03-01 13:47:30.876000',0.0016,61500.0,61800.0,0.28272,0.0984,'USDT',0.09888,'USDT','bb_lower','roi',5,'SPOT',0);
INSERT INTO orders VALUES (1,1,'buy','BTC/USDT',0,0.0016,61000.0,NULL,'24871100001','closed','BTC/USDT','limit','buy',61000.0,61000.0,0.0016,0.0016,0.0,97.6,NULL,'2024-03-01 10:05:02.512000','2024-03-01 10:05:31.207000','2024-03-01 10:05:31.207000',NULL,'rsi_cross');
INSERT INTO orders VALUES (2,1,'stoploss','BTC/USDT',0,0.0016,60390.0,NULL,'24871100002','canceled','BTC/USDT','stop_loss_limit','sell',59786.1,NULL,0.0016,0.0,0.0016,0.0,60390.0,'2024-03-01 10:05:32.004000',NULL,'2024-03-01 10:41:07.188000',NULL,'stoploss');
INSERT INTO orders VALUES (3,1,'sell','BTC/USDT',0,0.0016,61610.0,NULL,'24871100003','closed','BTC/USDT','limit','sell',61610.0,61610.0,0.0016,0.0016,0.0,98.576,NULL,'2024-03-01 10:41:07.402000','2024-03-01 10:41:52.660000','2024-03-01 10:41:52.660000',NULL,'roi');
INSERT INTO orders VALUES (4,2,'buy','ETH/USDT',0,0.03,3400.0,'cancelled due to timeout, partially filled','24871100004','canceled','ETH/USDT','limit','buy',3400.0,3400.0,0.03,0.012,0.018,40.8,NULL,'2024-03-01 10:15:03.377000','2024-03-01 10:19:44.102000','2024-03-01 10:25:04.120000',NULL,'rsi_cross');
INSERT INTO orders VALUES (5,2,'stoploss','ETH/USDT',0,0.012,3366.0,NULL,'24871100005','canceled','ETH/USDT','stop_loss_limit','sell',3332.34,NULL,0.012,0.0,0.012,0.0,3366.0,'2024-03-01 10:25:05.010000',NULL,'2024-03-01 11:00:02.300000',NULL,'stoploss');
INSERT INTO orders VALUES (6,2,'sell','ETH/USDT',0,0.012,3425.5,NULL,'24871100006','closed','ETH/USDT','market','sell',NULL,3424.1,0.012,0.012,0.0,41.0892,NULL,'2024-03-01 11:00:02.455000','2024-03-01 11:00:02.461000','2024-03-01 11:00:02.461000',NULL,'exit_signal');
INSERT INTO orders VALUES (7,3,'buy','BTC/USDT',0,0.0016,61200.0,NULL,'24871100007','closed','BTC/USDT','limit','buy',61200.0,61200.0,0.0016,0.0016,0.0,97.92,NULL,'2024-03-01 11:30:02.951000','2024-03-01 11:30:40.330000','2024-03-01 11:30:40.330000',NULL,'bb_lower');
INSERT INTO orders VALUES (8,3,'stoploss','BTC/USDT',0,0.0016,60588.0,NULL,'24871100008','closed','BTC/USDT','stop_loss_limit','sell',59982.12,60560.0,0.0016,0.0016,0.0,96.896,60588.0,'2024-03-01 11:30:41.020000','2024-03-01 12:13:20.905000','2024-03-01 12:13:20.905000',NULL,'stoploss');
INSERT INTO orders VALUES (9,4,'buy','ETH/USDT',0,0.03,3390.0,NULL,'24871100009','closed','ETH/USDT','limit','buy',3390.0,3390.0,0.03,0.03,0.0,101.7,NULL,'2024-03-01 12:00:02.630000','2024-03-01 12:03:12.118000','2024-03-01 12:03:12.118000',NULL,'rsi_cross');
INSERT INTO orders VALUES (10,4,'stoploss','ETH/USDT',0,0.03,3356.1,NULL,'24871100010','canceled','ETH/USDT','stop_loss_limit','sell',3322.54,NULL,0.03,0.0,0.03,0.0,3356.1,'2024-03-01 12:03:12.700000',NULL,'2024-03-01 12:25:02.010000',NULL,'stoploss');
INSERT INTO orders VALUES (11,4,'sell','ETH/USDT',0,0.03,3423.9,'cancelled due to timeout','24871100011','canceled','ETH/USDT','limit','sell',3423.9,NULL,0.03,0.0,0.03,0.0,NULL,'2024-03-01 12:25:02.214000',NULL,'2024-03-01 12:35:02.530000',NULL,'roi');
INSERT INTO orders VALUES (12,4,'sell','ETH/USDT',0,0.03,3421.0,NULL,'24871100012','closed','ETH/USDT','limit','sell',3421.0,3421.0,0.03,0.03,0.0,102.63,NULL,'2024-03-01 12:35:04.902000','2024-03-01 12:36:10.441000','2024-03-01 12:36:10.441000',NULL,'roi');
INSERT INTO orders VALUES (13,5,'buy','BTC/USDT',0,0.0016,61500.0,NULL,'24871100013','closed','BTC/USDT','limit','buy',61500.0,61500.0,0.0016,0.0016,0.0,98.4,NULL,'2024-03-01 13:00:03.402000','2024-03-01 13:08:41.007000','2024-03-01 13:08:41.007000',NULL,'bb_lower');
INSERT INTO orders VALUES (14,5,'stoploss','BTC/USDT',0,0.0016,60885.0,NULL,'24871100014','canceled','BTC/USDT','stop_loss_limit','sell',60276.15,NULL,0.0016,0.0,0.0016,0.0,60885.0,'2024-03-01 13:08:41.600000',NULL,'2024-03-01 13:45:02.050000',NULL,'stoploss');
INSERT INTO orders VALUES (15,5,'sell','BTC/USDT',0,0.0016,61800.0,NULL,'24871100015','closed','BTC/USDT','limit','sell',61800.0,61800.0,0.0016,0.0016,0.0,98.88,NULL,'2024-03-01 13:45:02.233000','2024-03-01 13:47:30.876000','2024-03-01 13:47:30.876000',NULL,'roi');
