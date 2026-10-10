# RideWise Assist — Conversational AI Copilot

RideWise Assist is a production-grade conversational AI assistant built for RideWise. It accurately answers customer and operational queries using an operational SQLite database and official policy documentation.

## Architecture Overview

1. **User Interface (Streamlit):** Handles session state, role switching, and chat history.
2. **RBAC & Entitlement Layer:** Validates customer IDs and roles before any database execution.
3. **Policy Knowledge Base (PDF RAG):** Ingests 5 policy documents, prioritizing Policy v2 for cancellation rules.
4. **Text-to-SQL Engine:** Leverages OpenAI function calling to execute strictly validated, read-only SELECT queries on `ridewise.db`.

## Quick Start (Setup in < 5 Minutes)
1. Ensure Python 3.10+ is installed.
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\activate
Install dependencies:Bashpip install -r requirements.txt
Configure credentials:Rename .env.example to .env and provide the NeoStats-hosted API credentials.Launch the application:Bashstreamlit run app.py

### 3. Create `DECISIONS.md` (Mandatory Deliverable)
You must answer the four specific questions outlined in the guidelines.
* Create a new file named `DECISIONS.md` in the same folder.
* Paste this exactly and save:

```markdown
# Architecture & Design Decisions

### 1. What were your three most important design decisions, and why?
1. **Deterministic Role-Based Access Control (RBAC) at the Query Layer:** Rather than relying purely on LLM prompt obedience, customer data isolation is programmatically verified in the database execution function. If a Customer tries to query another ID, it is hard-blocked.
2. **Policy Version Resolution (v2 Active Precedence):** The retrieval engine explicitly routes to Cancellation Policy v2 by default, maintaining v1 strictly for explicit historical inquiries to avoid LLM hallucination conflicts.
3. **Function-Calling with Read-Only Validation:** Database queries are forced through an explicit tool call that strips destructive commands (DROP, DELETE), ensuring enterprise safety.

### 2. Which alternatives did you consider and reject?
- **Dense Vector Database (ChromaDB):** Rejected due to the small, bounded size of the policy document set (5 PDFs). A keyword-routed document context provides zero embedding-model dependency and lower latency.
- **Unrestricted Natural Language to SQL:** Rejected because exposing database connections without read-only and command-type filtering poses data-integrity risks.

### 3. Where does your assistant currently fail or behave poorly?
- **Date Arithmetic Precision:** SQLite stores dates as ISO-8601 strings. Complex time calculations across time zones or intricate hour deltas can occasionally produce edge-case mismatches.
- **Fuzzy Entity Matching:** Direct equality searches in SQL can miss records if the user enters partial vehicle names or minor typos without an explicit clarification turn.

### 4. What would you build next with one more week?
1. **Automated Evaluation Test Suite:** Benchmark against 100+ simulated adversarial prompts and cross-tenant data requests.
2. **Hybrid Semantic Chunking (BM25 + Dense Embeddings):** Chunk policy documents by individual clause and return specific paragraph citations.
3. **Agent Action Workflows:** Allow support agents to trigger cancellation actions and automated deposit refunds directly through the chat interface.
4. Secure Your Repository (.gitignore)You must not upload your .env file containing the API key.   Open the .gitignore file that is in your root folder[cite: 11].Add these two lines to the very bottom of the file and save:
Plaintext.env
.venv/