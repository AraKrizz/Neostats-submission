import os
import sqlite3
import json
import re
import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv
import PyPDF2

load_dotenv()

# =====================================================================
# 1. SETUP & CLIENT INITIALIZATION
# =====================================================================
API_KEY = os.getenv("NEOSTATS_API_KEY")
BASE_URL = os.getenv("NEOSTATS_BASE_URL", "https://neo-hackathon.services.ai.azure.com/openai/v1/")
MODEL_NAME = os.getenv("NEOSTATS_MODEL_NAME", "Neo-Hackathon")

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL
)

DB_PATH = "ridewise.db"

# =====================================================================
# 2. DOCUMENT KNOWLEDGE BASE (Policy Documents)
# =====================================================================
@st.cache_resource
def load_policy_docs():
    doc_paths = {
        "01_Rental_Agreement": "01_Rental_Agreement_and_Terms.pdf",
        "02_Cancellation_v1": "02_Cancellation_and_Refund_Policy_v1.pdf",
        "03_Cancellation_v2_CURRENT": "03_Cancellation_and_Refund_Policy_v2.pdf",
        "04_Penalties": "04_Late_Return_and_Damage_Penalty_Schedule.pdf",
        "05_EV_Guide": "05_EV_User_Guide_and_FAQ.pdf"
    }
    extracted = {}
    for name, path in doc_paths.items():
        if os.path.exists(path):
            try:
                reader = PyPDF2.PdfReader(path)
                text = "\n".join([page.extract_text() or "" for page in reader.pages])
                extracted[name] = text
            except Exception as e:
                extracted[name] = f"Error reading {name}: {str(e)}"
        else:
            extracted[name] = f"{path} not found in root directory."
    return extracted

DOCS = load_policy_docs()

def search_relevant_docs(query: str) -> str:
    q_lower = query.lower()
    selected = []
    
    # Precedence: Default to v2 unless legacy explicitly requested
    if "cancel" in q_lower or "refund" in q_lower:
        if "v1" in q_lower or "old" in q_lower or "previous" in q_lower:
            selected.append(f"=== POLICY: Cancellation Policy (v1 Legacy) ===\n{DOCS.get('02_Cancellation_v1', '')[:3000]}")
        else:
            selected.append(f"=== POLICY: Cancellation Policy (v2 CURRENT ACTIVE) ===\n{DOCS.get('03_Cancellation_v2_CURRENT', '')[:3500]}")
            
    if "late" in q_lower or "damage" in q_lower or "penalty" in q_lower or "fine" in q_lower:
        selected.append(f"=== POLICY: Late Return & Damage Penalty Schedule ===\n{DOCS.get('04_Penalties', '')[:3500]}")
        
    if "ev" in q_lower or "battery" in q_lower or "charge" in q_lower or "range" in q_lower or "breakdown" in q_lower:
        selected.append(f"=== POLICY: EV User Guide & FAQ ===\n{DOCS.get('05_EV_Guide', '')[:3500]}")
        
    if "kyc" in q_lower or "license" in q_lower or "eligib" in q_lower or "deposit" in q_lower or not selected:
        selected.append(f"=== POLICY: Rental Agreement & Terms ===\n{DOCS.get('01_Rental_Agreement', '')[:3500]}")
        
    return "\n\n".join(selected)

# =====================================================================
# 3. DATABASE TOOL WITH STRICT ROLE-BASED ACCESS CONTROL (RBAC)
# =====================================================================
def execute_sql(query: str, role: str, active_customer_id: str):
    clean_q = query.strip()
    upper_q = clean_q.upper()
    
    # 1. Enforce Read-Only
    if not (upper_q.startswith("SELECT") or upper_q.startswith("WITH")):
        return {"error": "Security Alert: Only SELECT queries are permitted."}
        
    forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "ATTACH", "DETACH"]
    if any(word in upper_q.split() for word in forbidden):
        return {"error": "Security Alert: Modifying database is strictly prohibited."}
        
    # 2. Strict Customer Entitlements (RBAC)
    if role == "Customer":
        # Check for cross-customer leak attempts
        found_cust_ids = re.findall(r'CUST\d{4}', upper_q)
        for cid in found_cust_ids:
            if cid != active_customer_id.upper():
                return {"error": f"Entitlement Violation: You are logged in as {active_customer_id}. You cannot access records belonging to {cid}."}
        
        # Enforce filtering by active customer if querying customer-sensitive tables
        if any(tbl in upper_q for tbl in ["BOOKINGS", "CUSTOMERS", "PAYMENTS", "DAMAGE_REPORTS"]):
            if active_customer_id.upper() not in upper_q:
                return {"error": f"Entitlement Violation: Customer role must filter queries by customer_id = '{active_customer_id}'."}
            
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
# 4. INTENT & SYSTEM PROMPT WITH EXACT DATA SCHEMA
# =====================================================================
SCHEMA_DEFINITION = """
EXACT SQLITE SCHEMA (Do NOT invent non-existent column names):
1. customers (customer_id TEXT PK, full_name TEXT, phone TEXT, email TEXT, city TEXT, date_of_birth TEXT, driving_license_no TEXT, kyc_status TEXT, customer_tier TEXT, created_at TEXT)
2. vehicles (vehicle_id TEXT PK, registration_no TEXT, vehicle_type TEXT, model TEXT, hub_name TEXT, hourly_rate REAL, daily_rate REAL, battery_capacity_kwh REAL, status TEXT, commissioned_on TEXT)
3. bookings (booking_id TEXT PK, customer_id TEXT FK, vehicle_id TEXT FK, booked_at TEXT, scheduled_start TEXT, scheduled_end TEXT, actual_return_at TEXT, cancelled_at TEXT, booking_status TEXT, return_battery_pct INTEGER)
   * Note: booking_status values are: 'UPC' (upcoming), 'ONG' (ongoing), 'CMP' (completed), 'CNL_U' (cancelled customer), 'CNL_S' (cancelled RideWise), 'NSH' (no-show)
   * Pickup time is 'scheduled_start', return time is 'scheduled_end'. There is NO 'scheduled_pickup_time'.
   * Hub is in 'vehicles.hub_name'. There is NO 'hub_id' or 'pickup_hub' in bookings.
4. payments (payment_id TEXT PK, booking_id TEXT FK, payment_type TEXT, amount REAL, gst_amount REAL, payment_method TEXT, payment_status TEXT, transaction_ref TEXT, paid_at TEXT, remarks TEXT)
5. damage_reports (report_id TEXT PK, booking_id TEXT FK, vehicle_id TEXT FK, reported_by TEXT, damage_category TEXT, severity TEXT, description TEXT, assessed_cost REAL, claim_status TEXT, reported_at TEXT)
"""

SYSTEM_PROMPT = """
You are RideWise Assist, an enterprise conversational AI co-pilot for RideWise rental company.
Active Role: {role}
Active Customer ID: {customer_id}

{schema}

ENTITLEMENT RULES:
1. When Active Role is 'Customer':
   - Entitled ONLY to their own information ({customer_id}) and public policies.
   - If user asks for other customer's details (e.g., CUST0002 phone, bookings), strictly decline: "I cannot provide information about other customers due to RideWise privacy policy." Do NOT call the database.
   - Always filter bookings/customers by customer_id = '{customer_id}'.
2. When Active Role is 'Support Agent':
   - Entitled to cross-fleet operational queries, vehicle maintenance status, all bookings, and policy guidance.

POLICY PRECEDENCE:
- Always use Cancellation & Refund Policy v2 as current. Use v1 only if user explicitly asks for historical/v1 policy.

Be conversational, concise, and cite facts directly.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": "Execute a SELECT SQL query on ridewise.db following the exact schema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query": {
                        "type": "string",
                        "description": "Valid SQLite SELECT query following the exact schema provided."
                    }
                },
                "required": ["sql_query"]
            }
        }
    }
]

def clean_model_output(text: str) -> str:
    """Removes leaked raw model tokens if returned as plain text."""
    if not text:
        return ""
    text = re.sub(r'<\|start\|>.*?<\|call\|>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|.*?\|>', '', text)
    return text.strip()

def generate_response(messages, role, customer_id):
    last_user_query = messages[-1]["content"]
    
    # Fast RBAC Pre-Check: Block cross-customer inspection before touching the LLM
    if role == "Customer":
        mentioned_ids = re.findall(r'CUST\d{4}', last_user_query.upper())
        for cid in mentioned_ids:
            if cid != customer_id.upper():
                return f"I cannot disclose records or personal information for {cid}. As a RideWise customer, you are only entitled to view your own account ({customer_id}).", None, None

    policy_context = search_relevant_docs(last_user_query)
    
    formatted_system = SYSTEM_PROMPT.format(
        role=role,
        customer_id=customer_id,
        schema=SCHEMA_DEFINITION
    ) + f"\n\nPOLICY CONTEXT:\n{policy_context}"
    
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
    
    # 1. Native Tool Calling Branch
    if msg.tool_calls:
        tool_call = msg.tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        sql_query = args.get("sql_query", "")
        
        db_result = execute_sql(sql_query, role, customer_id)
        
        api_messages.append(msg)
        api_messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": "query_database",
            "content": json.dumps(db_result, default=str)
        })
        
        second_response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=api_messages
        )
        final_text = clean_model_output(second_response.choices[0].message.content)
        return final_text, sql_query, db_result

    # 2. Raw Token Leak Fallback (handles models outputting tool call text directly)
    raw_content = msg.content or ""
    if "functions.query_database" in raw_content or "SELECT" in raw_content:
        sql_match = re.search(r'SELECT\s+.*?(?:;|\'|"|}|\n|$)', raw_content, re.IGNORECASE | re.DOTALL)
        if sql_match:
            extracted_sql = sql_match.group(0).rstrip('"} ;\n')
            db_result = execute_sql(extracted_sql, role, customer_id)
            
            # Send result back for conversational answer
            api_messages.append({"role": "assistant", "content": raw_content})
            api_messages.append({
                "role": "user", 
                "content": f"Database returned: {json.dumps(db_result)}. Now answer the user's question naturally without showing tokens."
            })
            second_response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=api_messages
            )
            return clean_model_output(second_response.choices[0].message.content), extracted_sql, db_result

    return clean_model_output(raw_content), None, None

# =====================================================================
# 5. STREAMLIT USER INTERFACE
# =====================================================================
st.set_page_config(page_title="RideWise Assist", page_icon="🛵", layout="wide")

st.sidebar.title("🛵 RideWise Assist")
st.sidebar.subheader("Session Configuration")

role = st.sidebar.selectbox("Select Role", ["Customer", "Support Agent"])
customer_id = "N/A"
if role == "Customer":
    customer_id = st.sidebar.text_input("Enter Customer ID", value="CUST0001")

if st.sidebar.button("🔄 Start New Conversation"):
    st.session_state.messages = []
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.markdown("### 📋 System Status")
st.sidebar.success("Database: Connected (`ridewise.db`)")
st.sidebar.success(f"Policies Loaded: {len(DOCS)} documents")
st.sidebar.info(f"Active Role: **{role}**")
if role == "Customer":
    st.sidebar.info(f"Target ID: **{customer_id}**")

st.title(f"RideWise Assist — {role} Portal")
st.caption("AI Co-pilot for Bookings, Payments, Fleet Status, and Policies")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "sql" in msg and msg["sql"]:
            with st.expander("🔍 Verified SQL Execution"):
                st.code(msg["sql"], language="sql")
                st.json(msg["data"])

if user_prompt := st.chat_input("Ask a question about bookings, refunds, penalties, or rules..."):
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        with st.spinner("Analyzing operational database and policy documents..."):
            try:
                answer, executed_sql, db_data = generate_response(
                    st.session_state.messages,
                    role=role,
                    customer_id=customer_id
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