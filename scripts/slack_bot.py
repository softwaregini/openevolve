#!/usr/bin/env python3
"""
Entry point for the OpenEvolve Slack bot.

Usage:
    export SLACK_APP_TOKEN=xapp-...
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_DEFAULT_CHANNEL=#openevolve   # optional, for notify()
    python scripts/slack_bot.py

Install requirements first: pip install -e '.[slack]'
"""

from openevolve.integrations.slack import run

if __name__ == "__main__":
    run()
