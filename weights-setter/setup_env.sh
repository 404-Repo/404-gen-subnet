#!/bin/bash
set -e

# Install dependencies
pip install -r requirements.txt
pip install -e ../shared

# Get Python path
PYTHON_PATH=$(which python)

# Generate PM2 config
cat <<EOF > 404-weights-setter.config.js
const path = require('path');

module.exports = {
  apps: [{
    name: '404-weights-service',
    script: 'main.py',
    interpreter: '${PYTHON_PATH}',
    env: {
      PYTHONPATH: path.resolve(__dirname) + path.delimiter + path.resolve(__dirname, '..'),
    }
  }]
};
EOF

echo "[INFO] Setup complete. Run with: pm2 start 404-weights-setter.config.js"