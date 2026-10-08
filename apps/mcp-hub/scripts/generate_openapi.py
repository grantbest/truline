import json
import sys
import os

# Add src/ to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openapi_app import app

if __name__ == "__main__":
    print(json.dumps(app.openapi(), indent=2))
