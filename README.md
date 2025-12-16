## Robot VWAP

Робот для **Bybit USDT perpetual (linear swap)** на базе `ccxt`.

### Что делает
- Считает VWAP от выбранного якоря (Session/Week/Month/Year).
- Ставит сетку лимитных входов по уровням (в % от VWAP) в направлении **Long/Short**.
- При наличии позиции **перевыставляет TP/SL** от текущего VWAP.
- На каждой новой свече **перевыставляет входные лимитки**.
- Хранит состояние/конфиг в локальных JSON файлах.

### Важно
- Это **пример кода**, не финансовый совет.
- Перед реальной торговлей проверь на тестнете и на малых объёмах.

### Требования
- Python 3.10+

### Установка
```bash
python3 -m pip install -r requirements.txt
```

### Запуск
```bash
python3 bybit_vwap_strategy.py
```

### Запуск без ручного ввода (через переменные окружения)
```bash
export BYBIT_API_KEY="..."
export BYBIT_API_SECRET="..."
export BYBIT_SYMBOL="SOL/USDT:USDT"   # или SOLUSDT
export BYBIT_DIRECTION="Short"        # Long или Short
export BYBIT_TESTNET="true"           # true/false

python3 bybit_vwap_strategy.py
```

### Файлы состояния
- `strategy_config_*.json` — настройки стратегии (создаётся при первом запуске)
- `strategy_state_*.json` — состояние (ордера/флаги), чтобы переживать перезапуски

### Версии
Текущая версия: **0.1.0** (`bybit_vwap_strategy.__version__`).
