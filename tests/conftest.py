import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs" / "contracts" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "brokers" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "market_data" / "src"))
sys.path.insert(0, str(ROOT / "services" / "audit-logger" / "src"))
sys.path.insert(0, str(ROOT / "services" / "autonomy-orchestrator" / "src"))
sys.path.insert(0, str(ROOT / "services" / "approval-gateway" / "src"))
sys.path.insert(0, str(ROOT / "services" / "sentiment-aggregator" / "src"))
sys.path.insert(0, str(ROOT / "services" / "notification-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "strategy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "policy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "execution-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "portfolio-service" / "src"))
