import sys
import os

# Ensure src/ is in pythonpath
sys.path.insert(0, os.path.dirname(__file__))

from openapi_app import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
