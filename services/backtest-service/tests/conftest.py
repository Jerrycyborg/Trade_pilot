import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "libs" / "contracts" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "market_data" / "src"))
sys.path.insert(0, str(ROOT / "services" / "backtest-service" / "src"))
