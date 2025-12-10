# Weights Service

Sets miner weights on the subnet based on competition results from GitHub.

## Prerequisites

- Python 3.11+
- PM2
- Bittensor wallet
```bash
sudo apt install nodejs npm
sudo npm install -g pm2
```

## Setup

### 1. Create and activate virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies
```bash
./setup_env.sh
```

### 3. Configure environment

Copy the template and fill in your wallet details:
```bash
cp .env.template .env
```

Required variables:
- `WALLET_NAME` - wallet name
- `WALLET_HOTKEY` - wallet hotkey

See `.env.template` for additional options.

### 4. Run
```bash
pm2 start 404-weights-setter.config.js
```

## Management
```bash
pm2 status                    # check status
pm2 logs 404-weights-service  # view logs
pm2 restart 404-weights-service
pm2 stop 404-weights-service
```