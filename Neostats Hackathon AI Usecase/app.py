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
VECTOR_STORE_PATH = "vector_store"
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
3. bookings (booking_id TEXT PK, customer_id TEXT FK, vehicle_id TEXT FK, booked_at TEXT, scheduled_start TEXT, scheduled_end TEXT, actual_return_at TEXT, cancelled_at TEXT, booking_status TEXT (UPC = upcoming, ONG = ongoing, CMP = completed, CNL_U = cancelled by user, CNL_S = cancelled by staff, NSH = no-show), return_battery_pct INTEGER)
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

def clean_model_output(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<\|start\|>.*?<\|call\|>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|.*?\|>", "", text)
    return text.strip()

def generate_response(messages, role, customer_id):
    last_user_query = messages[-1]["content"]
    
    if role == "Customer":
        mentioned_ids = re.findall(r"CUST\d{4}", last_user_query.upper())
        for cid in mentioned_ids:
            if cid != customer_id.upper():
                return f"I cannot disclose records for {cid}.", None, None

    policy_context = search_vector_db(last_user_query)
    
    formatted_system = (
        SYSTEM_PROMPT.format(role=role, customer_id=customer_id, schema=SCHEMA_DEFINITION)
        + f"\n\nPOLICY CONTEXT (Retrieved from ChromaDB Vector Store):\n{policy_context}"
    )
    
    api_messages = [{"role": "system", "content": formatted_system}]
    for m in messages:
        if m["role"] in ["user", "assistant"]:
            api_messages.append({"role": m["role"], "content": m["content"]})
    
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=api_messages,
        tools=TOOLS,
        tool_choice="auto"
    )
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
        return clean_model_output(second_response.choices[0].message.content), sql_query, db_result

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
            return clean_model_output(second_response.choices[0].message.content), extracted_sql, db_result

    return clean_model_output(raw_content), None, None

# =====================================================================
# 6. STREAMLIT INTERFACE (Login, Register & Chat)
# =====================================================================
st.set_page_config(page_title="RideWise Assist", page_icon="🛵", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "messages" not in st.session_state:
    st.session_state.messages = []

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
            reg_cust_id = st.text_input("Customer ID (e.g., CUST0001, leave blank for Support Agent)")
            submitted_reg = st.form_submit_button("Register")
            
            if submitted_reg:
                if not reg_user or not reg_pass:
                    st.warning("Please fill in both username and password.")
                elif reg_role == "Customer" and not reg_cust_id.upper().startswith("CUST"):
                    st.error("Customer registrations must include a valid Customer ID (e.g. CUST0001).")
                else:
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
        st.rerun()
        
    if st.sidebar.button("🚪 Logout"):
        st.session_state.authenticated = False
        st.session_state.messages = []
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.success("Database: Connected (`ridewise.db`)")
    st.sidebar.success("Vector Store: Active (ChromaDB)")
    st.sidebar.info(f"Active Role: **{st.session_state.role}**")
    if st.session_state.role == "Customer":
        st.sidebar.info(f"Scoped Customer ID: **{st.session_state.customer_id}**")

    st.title(f"RideWise Assist — {st.session_state.role} Portal")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if "sql" in msg and msg["sql"]:
                with st.expander("🔍 Verified SQL Execution"):
                    st.code(msg["sql"], language="sql")
                    st.json(msg["data"])

    if user_prompt := st.chat_input("Ask about bookings, refunds, penalties, or EV guidelines..."):
        st.session_state.messages.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        with st.chat_message("assistant"):
            with st.spinner("Analyzing operational database and vector policies..."):
                try:
                    answer, executed_sql, db_data = generate_response(
                        st.session_state.messages,
                        role=st.session_state.role,
                        customer_id=st.session_state.customer_id
                    )
                    st.markdown(answer)
                    if executed_sql:
                        with st.expander("🔍 Verified SQL Execution"):
                            st.code(executed_sql, language="sql")
                            st.json(db_data)
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": answer,
                        "sql": executed_sql,
                        "data": db_data
                    })
                except Exception as e:
                    err_msg = f"⚠️ System Error: {str(e)}"
                    st.error(err_msg)
                    st.session_state.messages.append({"role": "assistant", "content": err_msg})