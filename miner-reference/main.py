import uvicorn

from miner_reference.service import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10006)
