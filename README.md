# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most exposed to weather disruption. Rankings and scores come from deterministic Python code; the LLM (OpenAI gpt-6.2-sol) plans which tools to call and explains the results.

> Work in progress. See [PLAN.md](PLAN.md) for the implementation plan.

## Quickstart (local)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then put your OPENAI_API_KEY in .env
```
