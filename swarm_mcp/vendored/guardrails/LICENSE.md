# trading-swarm-guardrails — vendored CHECKER subset (source-available)

Copyright (c) 2026 The 1.21 Initiative. All rights reserved.

These files are a subset of the privately developed trading-swarm-guardrails
rules layer, vendored at the commit SHA recorded in .github/pins.json.

You may use them as part of trading-swarm-mcp to validate orders, features,
genomes, and execution-cost assumptions locally. You may NOT:

1. redistribute these files standalone, or as part of a competing product;
2. modify them (the pin exists precisely so the checkers cannot drift);
3. use them to reconstruct the selection machinery that was deliberately
   excluded (objective scoring, deflated-Sharpe estimation, promotion gates).

The excluded selection machinery remains server-side and is not licensed.
