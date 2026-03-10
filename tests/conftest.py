import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs" / "contracts" / "src"))
sys.path.insert(0, str(ROOT / "services" / "strategy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "policy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "execution-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "portfolio-service" / "src"))
