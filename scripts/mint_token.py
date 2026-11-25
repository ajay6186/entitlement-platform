"""DEV ONLY: print a bearer token for a demo persona.  python -m scripts.mint_token support_lead [agent_id]"""
import sys

from src.core.security import PERSONAS, issue_persona_token

if len(sys.argv) < 2 or sys.argv[1] not in PERSONAS:
    sys.exit(f"usage: python -m scripts.mint_token <{'|'.join(PERSONAS)}> [agent_id]")
print(issue_persona_token(sys.argv[1], agent_id=sys.argv[2] if len(sys.argv) > 2 else None))
