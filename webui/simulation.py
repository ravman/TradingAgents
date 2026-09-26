"""Simulation LLM: runs the real TradingAgents graph with scripted reasoning.

Selecting the "simulation" provider in the dashboard swaps the language model for
this one. Every other part of the pipeline is real: the graph, the tool calls,
the Yahoo Finance / news data, the decision log and the paper ledger. It exists
so you can see the whole dashboard work, and test your setup, without spending
API credits. Its "reasoning" is canned text and its ratings are weighted by
recent price momentum -- it is NOT an analysis and must not be traded on.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from tradingagents.agents import schemas

RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]

# Per-run context set by the engine before the graph starts.
RUN_CTX: dict = {"ticker": "SPY", "date": datetime.now().strftime("%Y-%m-%d"),
                 "bias": 0.0, "delay": 0.6}


def momentum_bias(ticker: str, date: str) -> float:
    """20-day return up to the run date, used to lean the simulated call."""
    try:
        import yfinance as yf
        end = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        hist = yf.Ticker(ticker).history(start=(end - timedelta(days=40)).strftime("%Y-%m-%d"),
                                         end=end.strftime("%Y-%m-%d"))
        closes = hist["Close"].dropna()
        if len(closes) > 5:
            return float(closes.iloc[-1] / closes.iloc[-min(20, len(closes))] - 1)
    except Exception:
        pass
    return 0.0


def _pick_rating(rng: random.Random) -> str:
    b = RUN_CTX.get("bias", 0.0)
    # Shift the centre of the 5-tier scale by momentum, then add noise.
    centre = 2 - max(-2.0, min(2.0, b * 25))
    idx = int(round(max(0, min(4, rng.gauss(centre, 0.9)))))
    return RATINGS[idx]


_LINES = {
    "Market Analyst": "Price action and indicators for {t}: trend over the last month is {trend} "
                      "({pct:+.1%}). RSI and MACD are consistent with that read; volume is unremarkable.",
    "Sentiment Analyst": "Social and news tone on {t} is {tone}. Retail chatter follows price rather than leading it.",
    "News Analyst": "Macro backdrop: rates and inflation headlines dominate. No {t}-specific shock this week.",
    "Fundamentals Analyst": "{t} fundamentals: margins stable, balance sheet adequate, valuation {val} vs history.",
    "Bull Researcher": "Bull Analyst: Momentum of {pct:+.1%} and steady fundamentals support upside for {t}.",
    "Bear Researcher": "Bear Analyst: Valuation is {val} and macro risk is elevated; the upside case for {t} is priced in.",
    "Aggressive Analyst": "Aggressive Analyst: Size up. The trend is our friend and the risk/reward favours action.",
    "Conservative Analyst": "Conservative Analyst: Keep exposure small; drawdown risk outweighs the expected edge.",
    "Neutral Analyst": "Neutral Analyst: A measured position with a stop balances both views.",
}


def _text_for(node: str, rng: random.Random) -> str:
    t = RUN_CTX["ticker"]
    pct = RUN_CTX.get("bias", 0.0)
    fill = dict(t=t, pct=pct,
                trend="up" if pct > 0.01 else "down" if pct < -0.01 else "flat",
                tone="constructive" if pct > 0 else "cautious",
                val=rng.choice(["rich", "fair", "undemanding"]))
    body = _LINES.get(node, "Reviewed the inputs for {t}.").format(**fill)
    rating = _pick_rating(rng)
    return (f"[SIMULATED — not real analysis]\n\n{body}\n\n**Rating**: {rating}\n\n"
            f"FINAL TRANSACTION PROPOSAL: **{ {'Buy':'BUY','Overweight':'BUY','Hold':'HOLD'}.get(rating,'SELL') }**")


def _tool_args(props: set) -> dict:
    t, d = RUN_CTX["ticker"], RUN_CTX["date"]
    start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    args = {"symbol": t, "ticker": t, "curr_date": d, "start_date": start, "end_date": d,
            "indicator": "rsi", "look_back_days": 30, "limit": 5,
            "topic": "Federal Reserve rate decision", "freq": "quarterly"}
    return {k: v for k, v in args.items() if k in props}


class SimulationChatModel(BaseChatModel):
    tools: tuple = ()
    seed: int = Field(default_factory=lambda: random.randrange(1 << 30))

    @property
    def _llm_type(self) -> str:
        return "simulation"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        def make(_):
            time.sleep(RUN_CTX.get("delay", 0.6))
            rng = random.Random()
            r = _pick_rating(rng)
            tag = "[SIMULATED — not real analysis] "
            if schema is schemas.ResearchPlan:
                return schemas.ResearchPlan(recommendation=schemas.PortfolioRating(r),
                                            rationale=tag + "The side with the stronger evidence carried the debate.",
                                            strategic_actions="Scale in over several sessions; standard allocation.")
            if schema is schemas.TraderProposal:
                act = "Buy" if r in ("Buy", "Overweight") else "Hold" if r == "Hold" else "Sell"
                return schemas.TraderProposal(action=schemas.TraderAction(act), reasoning=tag + "Follows the research plan.",
                                              position_sizing="5% of portfolio")
            if schema is schemas.PortfolioDecision:
                return schemas.PortfolioDecision(rating=schemas.PortfolioRating(r),
                                                 executive_summary=tag + f"Final rating {r} for {RUN_CTX['ticker']}.",
                                                 investment_thesis="Simulated thesis weighted by recent momentum.")
            if schema is schemas.SentimentReport:
                return schemas.SentimentReport(overall_band=schemas.SentimentBand.NEUTRAL, overall_score=5.0,
                                               confidence="low", narrative=tag + "Mixed social tone.")
            raise NotImplementedError(schema)
        return RunnableLambda(make)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        time.sleep(RUN_CTX.get("delay", 0.6))
        node = ""
        if run_manager is not None:
            node = (getattr(run_manager, "metadata", {}) or {}).get("langgraph_node", "")
        rng = random.Random()
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = []
            for i, tool in enumerate(self.tools):
                props = set(tool.tool_call_schema.model_json_schema()["properties"])
                calls.append({"name": tool.name, "id": f"sim_{i}_{rng.randrange(1 << 20)}",
                              "args": _tool_args(props)})
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content=_text_for(node, rng),
                                usage_metadata={"input_tokens": rng.randint(1500, 6000),
                                                "output_tokens": rng.randint(200, 900),
                                                "total_tokens": 0})
        return ChatResult(generations=[ChatGeneration(message=message)])


class SimulationClient:
    def __init__(self, **_):
        self.model = SimulationChatModel()

    def get_llm(self):
        return self.model
