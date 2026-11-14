import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("helm.main:app", host="127.0.0.1", port=int(os.getenv("HELM_PORT", "8100")))
