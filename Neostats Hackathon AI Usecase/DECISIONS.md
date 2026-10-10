# Architecture & Design Decisions

### 1. What were your three most important design decisions, and why?
1. **Deterministic Role-Based Access Control (RBAC) at the Query Layer:** Rather than relying purely on LLM prompt obedience, customer data isolation is programmatically verified in the database execution function. If a Customer tries to query another ID, it is hard-blocked.
2. **Policy Version Resolution (v2 Active Precedence):** The retrieval engine explicitly routes to Cancellation Policy v2 by default, maintaining v1 strictly for explicit historical inquiries to avoid LLM hallucination conflicts.
3. **Function-Calling with Read-Only Validation:** Database queries are forced through an explicit tool call that strips destructive commands (DROP, DELETE, UPDATE), ensuring enterprise data safety.

### 2. Which alternatives did you consider and reject?
- **Dense Vector Database (ChromaDB/FAISS):** Rejected due to the small, bounded size of the policy document set (5 PDFs). A keyword-routed document context provides zero embedding-model dependency and much lower latency.
- **Unrestricted Natural Language to SQL:** Rejected because exposing database connections without read-only and command-type filtering poses massive data-integrity and security risks.

### 3. Where does your assistant currently fail or behave poorly?
- **Date Arithmetic Precision:** SQLite stores dates as ISO-8601 strings. Complex time calculations across time zones or intricate hour deltas can occasionally produce edge-case mismatches.
- **Fuzzy Entity Matching:** Direct equality searches in SQL can miss records if the user enters partial vehicle names or minor typos without an explicit clarification turn.

### 4. What would you build next with one more week?
1. **Automated Evaluation Test Suite:** Benchmark against 100+ simulated adversarial prompts, boundary-case refund requests, and cross-tenant data attempts.
2. **Hybrid Semantic Chunking (BM25 + Dense Embeddings):** Chunk policy documents by individual clause and return specific paragraph citations.
3. **Agent Action Workflows:** Allow support agents to trigger cancellation actions and automated deposit refunds directly through the chat interface.