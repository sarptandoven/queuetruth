-- Freqtrade live trades database (futures, one short trade) as SQL: the minimal columns the importer reads
-- (freqtrade/persistence/trade_model.py at commit 1c0d709401b5). Exchange order ids are Binance's numeric ids, so
-- order_date is the exchange's order time (ccxt timestamp). The entry's average (3399.9500000000003) is a value as
-- floating point arithmetic produces it; the exit is a market order.
CREATE TABLE trades (
    id INTEGER NOT NULL, exchange VARCHAR(25) NOT NULL, pair VARCHAR(25) NOT NULL, is_open BOOLEAN NOT NULL,
    open_date DATETIME NOT NULL, close_date DATETIME, fee_open_cost FLOAT, fee_open_currency VARCHAR(25),
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
INSERT INTO trades VALUES (1,'binanceusdm','ETH/USDT:USDT',0,'2024-03-01 09:00:01.204000','2024-03-01 09:20:07.551000',0.0407994,'USDT',0.0405,'USDT','short_rsi','exit_signal',5,'FUTURES',1);
INSERT INTO orders VALUES (1,1,'sell','ETH/USDT:USDT',0,0.12,3400.0,NULL,'8389765612345678901','closed','ETH/USDT:USDT','limit','sell',3400.0,3399.9500000000003,0.12,0.12,0.0,407.994,NULL,'2024-03-01 09:00:01.204000','2024-03-01 09:00:09.877000','2024-03-01 09:00:09.877000',NULL,'short_rsi');
INSERT INTO orders VALUES (2,1,'buy','ETH/USDT:USDT',0,0.12,3375.0,NULL,'8389765612345679555','closed','ETH/USDT:USDT','market','buy',NULL,3375.0,0.12,0.12,0.0,405.0,NULL,'2024-03-01 09:20:07.312000','2024-03-01 09:20:07.551000','2024-03-01 09:20:07.551000',NULL,'exit_signal');
INSERT INTO orders VALUES (3,1,'buy','ETH/USDT:USDT',0,0.12,3375.0,'cancelled on exchange','8389765612345679001','canceled','ETH/USDT:USDT','limit','buy',3380.0,NULL,0.12,0.0,0.12,0.0,NULL,'2024-03-01 09:15:00.950000',NULL,'2024-03-01 09:18:44.020000',NULL,'exit_signal');
INSERT INTO orders VALUES (4,1,'stoploss','ETH/USDT:USDT',0,0.12,3434.0,NULL,'8389765612345678950','canceled','ETH/USDT:USDT','stop_market','buy',NULL,NULL,0.12,0.0,0.12,0.0,3434.0,'2024-03-01 09:00:10.100000',NULL,'2024-03-01 09:20:07.700000',NULL,'stoploss');
