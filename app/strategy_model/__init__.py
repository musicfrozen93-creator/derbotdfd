"""Bot-specific strategy model reconstructed from the 10 DBot XML files.

The model reproduces, for each bot, the entry rule, the contract and prediction actually bought
in each state, the trade duration, the stake progression after losses and the bot's own stop
rules. Evidence for every rule is cited in :mod:`app.strategy_model.bots`.

The strategy YAML files in ``strategies/`` are not modified; at start-up the model is
cross-checked against them (contract type, prediction and duration of every leg).
"""
