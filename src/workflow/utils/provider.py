"""LLM clients for the workflow agents, one factory per provider.

Every factory reads its model name from the environment so a
quota-exhausted model can be swapped without a code edit: GOOGLE_MODEL,
GROQ_MODEL, and (for the failure rewriter) REWRITE_MODEL. Keys come from
GEMINI_API_KEY, groq_key (or GROQ_API_KEY), and OPENROUTER_API_KEY.
"""
import os

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_openrouter import ChatOpenRouter

load_dotenv()


def gemini_model():
    """Judgment tasks: the scenario planner and the coverage auditor."""
    return ChatGoogleGenerativeAI(
        model=os.getenv("GOOGLE_MODEL", "gemini-3.5-flash"),
        google_api_key=os.getenv("GEMINI_API_KEY"),
        temperature=0.2,
        max_output_tokens=7900,
    )


def groq_model():
    """Payload construction: the test builder."""
    return ChatGroq(
        model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
        api_key=os.getenv("groq_key") or os.getenv("GROQ_API_KEY"),
        temperature=0,
        max_tokens=8000,
    )


def openrouter_model():
    """The failure rewriter, kept on its own provider and quota."""
    return ChatOpenRouter(
        model=os.getenv("REWRITE_MODEL", "deepseek/deepseek-v4-flash"),
        temperature=0,
        max_tokens=8000,
        reasoning={"effort": "low"},
    )
