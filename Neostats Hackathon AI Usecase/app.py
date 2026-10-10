import os
import sqlite3
import json
import re
import hashlib
import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import PyPDF2
import chromadb
from chromadb.utils import embedding_functions

# Ensure paths resolve properly regardless of execution folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# =====================================================================
# 1. SETUP & CLIENT INITIALIZATION
# =====================================================================
API_KEY = os.getenv("NEOSTATS_API_KEY")
BASE_URL = os.getenv("NEOSTATS_BASE_URL")
MODEL_NAME = os.getenv("NEOSTATS_MODEL_NAME")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
DB_PATH = os.path.join(BASE_DIR, "ridewise.db")
AUTH_DB_PATH = os.path.join(BASE_DIR, "auth.db")
VECTOR_STORE_PATH = "vector_store"  # Local relative directory

# =====================================================================
# 2. AUTHENTICATION SYSTEM (Hashed SQLite Storage)
# =====================================================================
def init_auth_db():
    conn = sqlite3.connect(AUTH_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            role TEXT,
            customer_id TEXT
        )
    """)
    conn.commit()
    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def register_user(username: str, password: str, role: str, customer_id: str):
    conn = sqlite3.connect(AUTH_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, customer_id) VALUES (?, ?, ?, ?)",
            (username.strip(), hash_password(password), role, customer_id.strip().upper())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def authenticate_user(username: str, password: str):
    conn = sqlite3.connect(AUTH_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, customer_id FROM users WHERE username = ? AND password_hash = ?",
        (username.strip(), hash_password(password))
    )
    user = cursor.fetchone()
    conn.close()
    return user

init_auth_db()

# =====================================================================
# 3. ADVANCED RAG (ChromaDB Vector Store)
# =====================================================================
chroma_client = chromadb.PersistentClient(path=VECTOR_STORE_PATH)
sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
collection = chroma_client.get_or_create_collection(name="ridewise_policies", embedding_function=sentence_transformer_ef)

@st.cache_resource
def initialize_vector_db():
    if collection.count() == 0:
        doc_paths = {
            "01_Rental_Agreement": os.path.join(BASE_DIR, "01_Rental_Agreement_and_Terms.pdf"),
            "02_Cancellation_v1_LEGACY": os.path.join(BASE_DIR, "02_Cancellation_and_Refund_Policy_v1.pdf"),
            "03_Cancellation_v2_CURRENT": os.path.join(BASE_DIR, "03_Cancellation_and_Refund_Policy_v2.pdf"),
            "04_Penalties": os.path.join(BASE_DIR, "04_Late_Return_and_Damage_Penalty_Schedule.pdf"),
            "05_EV_Guide": os.path.join(BASE_DIR, "05_EV_User_Guide_and_FAQ.pdf")
        }
        chunk_id = 0
        for doc_name, path in doc_paths.items():
            if os.path.exists(path):
                reader = PyPDF2.PdfReader(path)
                full_text = "\n".join([page.extract_text() or "" for page in reader.pages])
                paragraphs = [p.strip() for p in full_text.split("\n\n") if len(p.strip()) > 40]
                
                for para in paragraphs:
                    collection.add(
                        documents=[para],
                        metadatas=[{"source": doc_name}],
                        ids=[f"chunk_{chunk_id}"]
                    )
                    chunk_id += 1

initialize_vector_db()

def search_vector_db(query: str) -> str:
    results = collection.query(query_texts=[query], n_results=4)
    context_pieces = []
    if results.get("documents") and results["documents"]:
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            source_name = meta.get("source", "Document")
            if "v1_LEGACY" in source_name and "old" not in query.lower() and "v1" not in query.lower():
                continue
            context_pieces.append(f"=== Policy Source: {source_name} ===\n{doc}")
    return "\n\n".join(context_pieces)

# =====================================================================
# 4. DATABASE TOOL & STRICT RBAC
# =====================================================================
def execute_sql(query: str, role: str, active_customer_id: str):
    clean_q = query.strip()
    upper_q = clean_q.upper()
    
    if not (upper_q.startswith("SELECT") or upper_q.startswith("WITH")):
        return {"error": "Security Alert: Only read-only SELECT queries are permitted."}
        
    forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE"]
    if any(word in upper_q.split() for word in forbidden):
        return {"error": "Security Alert: Modifying the operational database is strictly prohibited."}
        
    if role == "Customer":
        found_cust_ids = re.findall(r"CUST\d{4}", upper_q)
        for cid in found_cust_ids:
            if cid != active_customer_id.upper():
                return {"error": f"Entitlement Violation: You are not authorized to access records for {cid}."}
        
        if any(tbl in upper_q for tbl in ["BOOKINGS", "CUSTOMERS", "PAYMENTS", "DAMAGE_REPORTS"]):
            if active_customer_id.upper() not in upper_q:
                return {"error": f"Entitlement Violation: Customer queries must be scoped to your customer_id = '{active_customer_id}'."}
            
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(clean_q)
        rows = cursor.fetchall()
        conn.close()
        return [dict(ix) for ix in rows][:20]
    except Exception as e:
        return {"error": f"SQL Execution Error: {str(e)}"}

# =====================================================================
# 5. RESPONSE GENERATION & INTENT ROUTING
# =====================================================================
SCHEMA_DEFINITION = """
EXACT SQLITE SCHEMA:
1. customers (customer_id TEXT PK, full_name TEXT, phone TEXT, email TEXT, city TEXT, date_of_birth TEXT, driving_license_no TEXT, kyc_status TEXT, customer_tier TEXT, created_at TEXT)
2. vehicles (vehicle_id TEXT PK, registration_no TEXT, vehicle_type TEXT, model TEXT, hub_name TEXT, hourly_rate REAL, daily_rate REAL, battery_capacity_kwh REAL, status TEXT, commissioned_on TEXT)
3. bookings (booking_id TEXT PK, customer_id TEXT FK, vehicle_id TEXT FK, booked_at TEXT, scheduled_start TEXT, scheduled_end TEXT, actual_return_at TEXT, cancelled_at TEXT, booking_status TEXT, return_battery_pct INTEGER)
   -> Note: booking_status uses codes: 'UPC' (Upcoming), 'CMP' (Completed), 'CNL' (Cancelled), 'ONG' (Ongoing).
4. payments (payment_id TEXT PK, booking_id TEXT FK, payment_type TEXT, amount REAL, gst_amount REAL, payment_method TEXT, payment_status TEXT, transaction_ref TEXT, paid_at TEXT, remarks TEXT)
5. damage_reports (report_id TEXT PK, booking_id TEXT FK, vehicle_id TEXT FK, reported_by TEXT, damage_category TEXT, severity TEXT, description TEXT, assessed_cost REAL, claim_status TEXT, reported_at TEXT)
"""

SYSTEM_PROMPT = """
You are RideWise Assist, an enterprise conversational AI co-pilot.
Active Role: {role}
Active Customer ID: {customer_id}

{schema}

ENTITLEMENT RULES:
1. 'Customer': Entitled ONLY to their own information ({customer_id}). Do NOT run database lookups for other users.
2. 'Support Agent': Entitled to operational queries across all vehicles and bookings.

CRITICAL OUTPUT FORMATTING:
At the very end of your final response, ALWAYS provide 3 short, helpful follow-up questions the user could ask next.
You MUST format this exactly on a new line like this:
SUGGESTIONS: [Question 1] | [Question 2] | [Question 3]
"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_database",
        "description": "Execute a SELECT SQL query on ridewise.db following the exact schema.",
        "parameters": {
            "type": "object",
            "properties": {"sql_query": {"type": "string", "description": "Valid SQLite SELECT query."}},
            "required": ["sql_query"]
        }
    }
}]

def clean_and_extract_suggestions(text: str):
    if not text:
        return "", []
    text = re.sub(r"<\|start\|>.*?<\|call\|>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|.*?\|>", "", text)
    
    suggestions = []
    if "SUGGESTIONS:" in text:
        parts = text.split("SUGGESTIONS:")
        text = parts[0].strip()
        raw_sugs = parts[1].strip()
        suggestions = [s.strip("[]* ") for s in raw_sugs.split("|") if s.strip()]
        
    return text.strip(), suggestions[:3]

def generate_response(messages, role, customer_id):
    last_user_query = messages[-1]["content"]
    
    if role == "Customer":
        mentioned_ids = re.findall(r"CUST\d{4}", last_user_query.upper())
        for cid in mentioned_ids:
            if cid != customer_id.upper():
                return f"I cannot disclose records for {cid}.", None, None, []

    policy_context = search_vector_db(last_user_query)
    
    formatted_system = (
        SYSTEM_PROMPT.format(role=role, customer_id=customer_id, schema=SCHEMA_DEFINITION)
        + f"\n\nPOLICY CONTEXT (Retrieved from ChromaDB Vector Store):\n{policy_context}"
    )
    
    api_messages = [{"role": "system", "content": formatted_system}]
    for m in messages:
        if m["role"] in ["user", "assistant"]:
            api_messages.append({"role": m["role"], "content": m["content"]})
    
    response = client.chat.completions.create(model=MODEL_NAME, messages=api_messages, tools=TOOLS, tool_choice="auto")
    msg = response.choices[0].message
    
    if msg.tool_calls:
        tool_call = msg.tool_calls[0]
        sql_query = json.loads(tool_call.function.arguments).get("sql_query", "")
        db_result = execute_sql(sql_query, role, customer_id)
        
        api_messages.extend([
            msg,
            {"role": "tool", "tool_call_id": tool_call.id, "name": "query_database", "content": json.dumps(db_result, default=str)}
        ])
        second_response = client.chat.completions.create(model=MODEL_NAME, messages=api_messages)
        final_txt, sugs = clean_and_extract_suggestions(second_response.choices[0].message.content)
        return final_txt, sql_query, db_result, sugs

    raw_content = msg.content or ""
    if "functions.query_database" in raw_content or "SELECT" in raw_content:
        sql_match = re.search(r"SELECT\s+.*?(?:;|\'|\"|\}|\n|$)", raw_content, re.IGNORECASE | re.DOTALL)
        if sql_match:
            extracted_sql = sql_match.group(0).rstrip('"} ;\n')
            db_result = execute_sql(extracted_sql, role, customer_id)
            api_messages.extend([
                {"role": "assistant", "content": raw_content},
                {"role": "user", "content": f"Database output: {json.dumps(db_result)}. Provide a concise natural language response to the user."}
            ])
            second_response = client.chat.completions.create(model=MODEL_NAME, messages=api_messages)
            final_txt, sugs = clean_and_extract_suggestions(second_response.choices[0].message.content)
            return final_txt, extracted_sql, db_result, sugs

    final_txt, sugs = clean_and_extract_suggestions(raw_content)
    return final_txt, None, None, sugs

# =====================================================================
# 6. STREAMLIT INTERFACE (Login, Register & Chat)
# =====================================================================
st.set_page_config(page_title="RideWise Assist", page_icon="🛵", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "messages" not in st.session_state:
    st.session_state.messages = []
if "suggestions" not in st.session_state:
    st.session_state.suggestions = []

if not st.session_state.authenticated:
    st.title("🔒 RideWise Secure Portal")
    
    tab_login, tab_register = st.tabs(["Login", "Register Account"])
    
    with tab_login:
        with st.form("login_form"):
            st.subheader("Account Sign In")
            login_user = st.text_input("Username")
            login_pass = st.text_input("Password", type="password")
            submitted_login = st.form_submit_button("Sign In")
            
            if submitted_login:
                user_data = authenticate_user(login_user, login_pass)
                if user_data:
                    st.session_state.authenticated = True
                    st.session_state.role = user_data[0]
                    st.session_state.customer_id = user_data[1]
                    st.rerun()
                else:
                    st.error("Invalid username or password.")
                    
    with tab_register:
        with st.form("register_form"):
            st.subheader("Create a Verified Account")
            reg_user = st.text_input("Choose Username")
            reg_pass = st.text_input("Choose Password", type="password")
            reg_role = st.selectbox("Role", ["Customer", "Support Agent"])
            reg_cust_id = st.text_input("Customer ID (e.g., CUST0001, leave blank for Agent)")
            submitted_reg = st.form_submit_button("Register")
            
            if submitted_reg:
                assigned_id = reg_cust_id if reg_role == "Customer" else "AGENT_ADMIN"
                created = register_user(reg_user, reg_pass, reg_role, assigned_id)
                if created:
                    st.success("Account created successfully. Switch to the Login tab to sign in.")
                else:
                    st.error("Username already registered.")

else:
    st.sidebar.title("🛵 RideWise Assist")
    
    if st.sidebar.button("🔄 Start New Conversation"):
        st.session_state.messages = []
        st.session_state.suggestions = []
        st.rerun()
        
    if st.sidebar.button("🚪 Logout"):
        st.session_state.authenticated = False
        st.session_state.messages = []
        st.session_state.suggestions = []
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.success("Database: Connected (`ridewise.db`)")
    st.sidebar.success("Vector Store: Active (ChromaDB)")
    st.sidebar.info(f"Active Role: **{st.session_state.role}**")
    if st.session_state.role == "Customer":
        st.sidebar.info(f"Scoped Customer ID: **{st.session_state.customer_id}**")

    # Enterprise Footer
    st.sidebar.markdown("---")
    st.sidebar.caption("© 2026 RideWise Assist Enterprise | v2.2.0")
    st.sidebar.caption("🔒 Secured by Role-Based Access Control")

    st.title(f"RideWise Assist — {st.session_state.role} Portal")

    # Auto-Welcome Message using Customer ID
    if len(st.session_state.messages) == 0:
        if st.session_state.role == "Customer":
            welcome_text = f"Welcome back, **{st.session_state.customer_id}**! 🛵 I have securely loaded your account details. How can I assist you with your rentals today?"
            st.session_state.suggestions = [
                "What are my upcoming bookings?",
                "Explain the late return penalty",
                "How do I start my EV scooter?"
            ]
        else:
            welcome_text = "Welcome to the Support Agent Portal. 🛠️ I can help you monitor fleet maintenance, look up global bookings, or review company policies."
            st.session_state.suggestions = [
                "Show vehicles in MAINTENANCE",
                "List all recent cancellations",
                "What is the security deposit policy?"
            ]
            
        st.session_state.messages.append({"role": "assistant", "content": welcome_text})

    # Render Chat History with Custom Avatars
    for msg in st.session_state.messages:
        avatar_icon = "🛵" if msg["role"] == "assistant" else "👤"
        with st.chat_message(msg["role"], avatar=avatar_icon):
            st.markdown(msg["content"])
            if "sql" in msg and msg["sql"]:
                with st.expander("🔍 Verified SQL Execution"):
                    st.code(msg["sql"], language="sql")
                    st.json(msg["data"])

    # Input capturing (Chat Box OR Clickable Suggestion Buttons)
    prompt_to_process = None

    if st.session_state.suggestions:
        st.markdown("**Suggested Actions:**")
        cols = st.columns(len(st.session_state.suggestions))
        for i, sug in enumerate(st.session_state.suggestions):
            if cols[i].button(sug, key=f"sug_btn_{len(st.session_state.messages)}_{i}"):
                prompt_to_process = sug
                st.session_state.suggestions = [] 

    chat_val = st.chat_input("Ask about bookings, refunds, penalties, or EV guidelines...")
    if chat_val:
        prompt_to_process = chat_val
        st.session_state.suggestions = []

    # Process the Input
    if prompt_to_process:
        st.session_state.messages.append({"role": "user", "content": prompt_to_process})
        
        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt_to_process)

        with st.chat_message("assistant", avatar="🛵"):
            with st.spinner("Analyzing operational database and vector policies..."):
                try:
                    answer, executed_sql, db_data, new_sugs = generate_response(
                        st.session_state.messages,
                        role=st.session_state.role,
                        customer_id=st.session_state.customer_id
                    )
                    st.markdown(answer)
                    if executed_sql:
                        st.toast("Secure Database Query Executed", icon="🔒")
                        with st.expander("🔍 Verified SQL Execution"):
                            st.code(executed_sql, language="sql")
                            st.json(db_data)
                            
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": answer,
                        "sql": executed_sql,
                        "data": db_data
                    })
                    st.session_state.suggestions = new_sugs
                    st.rerun()
                except Exception as e:
                    err_msg = f"⚠️ System Error: {str(e)}"
                    st.error(err_msg)
                    st.session_state.messages.append({"role": "assistant", "content": err_msg})