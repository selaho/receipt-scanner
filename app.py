import streamlit as st
import base64
from PIL import Image
import io
import json
from groq import Groq
import os
from dotenv import load_dotenv
import pandas as pd
import fitz
from datetime import datetime, date
import sqlite3
import hashlib

# ---------- Load environment variables ----------
load_dotenv()

# ---------- Page config ----------
st.set_page_config(page_title="Receipt Scanner with AI", layout="wide")

# ---------- Configuration constants ----------
MAX_SCANS_PER_DAY = 999999
MAX_FILES_PER_SUBMISSION = 50

# ---------- Helper: format numbers with commas and two decimals ----------
def format_number(value, as_currency=False):
    """
    Format a number with thousands separators and exactly two decimal places.
    If as_currency is True, add a '$' prefix.
    Returns a string.
    """
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        # Always format with two decimal places and thousands separators
        formatted = f"{value:,.2f}"
        if as_currency:
            return f"${formatted}"
        return formatted
    return str(value)

# ---------- Database helper functions ----------
DB_PATH = "receipt_scanner.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            items_json TEXT NOT NULL,
            subtotal REAL,
            tax REAL,
            total REAL,
            tip REAL,
            tip_percentage REAL,
            timestamp TEXT NOT NULL,
            image_bytes BLOB,
            raw_json TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_limits (
            user_id INTEGER NOT NULL,
            scan_date TEXT NOT NULL,
            scan_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, scan_date),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.close()

def migrate_db():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT tip, tip_percentage FROM scans LIMIT 1")
    except sqlite3.OperationalError as e:
        if "no such column" in str(e):
            try:
                c.execute("ALTER TABLE scans ADD COLUMN tip REAL")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE scans ADD COLUMN tip_percentage REAL")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        else:
            raise
    conn.close()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_val):
    return hash_password(password) == hash_val

def authenticate_user(username, password):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row and verify_password(password, row["password_hash"]):
        return row["id"]
    return None

def save_scan(user_id, entry):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO scans (
            user_id, filename, items_json, subtotal, tax, total, tip, tip_percentage,
            timestamp, image_bytes, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        entry["filename"],
        json.dumps(entry["items"]),
        entry.get("subtotal"),
        entry.get("tax"),
        entry.get("total"),
        entry.get("tip"),
        entry.get("tip_percentage"),
        entry["timestamp"],
        entry.get("image_bytes"),
        json.dumps(entry.get("raw_json", {}))
    ))
    scan_id = c.lastrowid
    conn.commit()
    conn.close()
    return scan_id

def update_scan(scan_id, entry):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE scans
        SET items_json = ?, subtotal = ?, tax = ?, total = ?, tip = ?, tip_percentage = ?, raw_json = ?
        WHERE id = ?
    ''', (
        json.dumps(entry["items"]),
        entry.get("subtotal"),
        entry.get("tax"),
        entry.get("total"),
        entry.get("tip"),
        entry.get("tip_percentage"),
        json.dumps(entry.get("raw_json", {})),
        scan_id
    ))
    conn.commit()
    conn.close()

def delete_scan(scan_id, user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM scans WHERE id = ? AND user_id = ?", (scan_id, user_id))
    conn.commit()
    conn.close()

def get_scan_count_today(user_id):
    today = date.today().isoformat()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT scan_count FROM scan_limits WHERE user_id = ? AND scan_date = ?", (user_id, today))
    row = c.fetchone()
    conn.close()
    return row["scan_count"] if row else 0

def increment_scan_count(user_id):
    today = date.today().isoformat()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO scan_limits (user_id, scan_date, scan_count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, scan_date) DO UPDATE SET scan_count = scan_count + 1
    ''', (user_id, today))
    conn.commit()
    conn.close()

# ---------- Helper: process one file ----------
def process_single_file(uploaded_file, user_id):
    if get_scan_count_today(user_id) >= MAX_SCANS_PER_DAY:
        raise Exception(f"Daily scan limit ({MAX_SCANS_PER_DAY}) reached. Please try again tomorrow.")

    if uploaded_file.type == "application/pdf":
        pdf_bytes = uploaded_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        images = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=150)
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            images.append(img)
        doc.close()
        if len(images) == 1:
            image = images[0]
        else:
            total_height = sum(img.height for img in images)
            max_width = max(img.width for img in images)
            combined = Image.new('RGB', (max_width, total_height), color='white')
            y_offset = 0
            for img in images:
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                combined.paste(img, (0, y_offset))
                y_offset += img.height
            image = combined
    else:
        image = Image.open(uploaded_file)
        if image.mode in ('RGBA', 'LA', 'P'):
            image = image.convert("RGB")

    max_width = 2000
    if image.width > max_width:
        ratio = max_width / image.width
        new_size = (max_width, int(image.height * ratio))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    img_bytes_io = io.BytesIO()
    image.save(img_bytes_io, format="JPEG", quality=85)
    image_bytes = img_bytes_io.getvalue()

    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    prompt = (
        "You are a receipt parser. Extract the following from the receipt image:\n"
        "1. List of items with their prices.\n"
        "2. Subtotal (before tax and tip).\n"
        "3. Tax amount.\n"
        "4. The final total paid – this is the amount the customer actually pays. It is usually at the bottom of the receipt and may be labeled 'TOTAL', 'Amount Due', or 'Total'. Look for the last total amount on the receipt.\n"
        "5. If there is a separate line for 'TIP' or 'Gratuity' that is included in the final total, extract that tip amount. Also, if a percentage is shown next to the tip, extract that as a number (e.g., 15, 20).\n"
        "IMPORTANT: Only extract the actual charged tip, not the suggested options. Do not compute the total yourself; just extract what is written on the receipt.\n"
        "Return the result as valid JSON with keys: 'items', 'subtotal', 'tax', 'total', 'tip', 'tip_percentage'.\n"
        "All monetary values must be numbers. If a field is missing, set it to null. Do not include any other text."
    )

    client = st.session_state.groq_client

    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_base64}"
                            }
                        }
                    ]
                }
            ],
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            temperature=0,
            max_tokens=1024,
            response_format={"type": "json_object"}
        )
    except Exception as e:
        if "429" in str(e):
            raise Exception("Groq rate limit reached. Please wait a moment and try again.")
        elif "json_validate_failed" in str(e):
            raise Exception("The AI returned invalid JSON. You can manually edit the receipt after saving.")
        else:
            raise e

    response_text = chat_completion.choices[0].message.content
    data = json.loads(response_text)

    items = data.get("items")
    if items is None:
        items = []
    elif not isinstance(items, list):
        items = []

    subtotal = data.get("subtotal")
    tax = data.get("tax")
    total = data.get("total")
    tip = data.get("tip")
    tip_percentage = data.get("tip_percentage")

    # Compute tip percentage if missing
    if tip_percentage is None and tip is not None and subtotal is not None and subtotal > 0:
        tip_percentage = round((tip / subtotal) * 100, 1)

    result = {
        "filename": uploaded_file.name,
        "items": items,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "tip": tip,
        "tip_percentage": tip_percentage,
        "raw_json": data,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "image_bytes": image_bytes
    }
    return result

# ---------- Generate detailed CSV with formatted numbers ----------
def generate_detailed_csv(entry):
    lines = []
    lines.append(f"Filename,{entry['filename']}")
    lines.append(f"Timestamp,{entry['timestamp']}")
    lines.append("")
    lines.append("Item,Price")
    for item in entry.get("items", []):
        price = item.get("price")
        lines.append(f"{item.get('name', '')},{format_number(price, as_currency=False)}")
    lines.append("")
    lines.append(f"Subtotal,{format_number(entry.get('subtotal'), as_currency=False)}")
    lines.append(f"Tax,{format_number(entry.get('tax'), as_currency=False)}")
    if entry.get("tip") is not None:
        lines.append(f"Tip,{format_number(entry['tip'], as_currency=False)}")
        if entry.get("tip_percentage") is not None:
            lines.append(f"Tip Percentage,{format_number(entry['tip_percentage'], as_currency=False)}")
    lines.append(f"Total (final),{format_number(entry.get('total'), as_currency=False)}")
    return "\n".join(lines)

# ---------- Main ----------
def main():
    # Session state
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.session_state.user_id = None
        st.session_state.username = None
    if "groq_client" not in st.session_state:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            st.error("GROQ_API_KEY not set in environment or .env file.")
            st.stop()
        st.session_state.groq_client = Groq(api_key=api_key)
    if "history" not in st.session_state:
        st.session_state.history = []
    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = {}
    if "img_width" not in st.session_state:
        st.session_state.img_width = {}
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0
    if "show_history" not in st.session_state:
        st.session_state.show_history = False
    if "expanded_rows" not in st.session_state:
        st.session_state.expanded_rows = {}

    init_db()
    migrate_db()

    # --- Force admin account ---
    conn = get_db_connection()
    c = conn.cursor()
    admin_hash = hashlib.sha256("pa$$4Admin".encode()).hexdigest()
    c.execute("INSERT OR REPLACE INTO users (id, username, password_hash) VALUES (1, 'admin', ?)", (admin_hash,))
    conn.commit()
    conn.close()

    # ---------- Login ----------
    if not st.session_state.authenticated:
        st.title("🔐 Login")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")
            if submitted:
                user_id = authenticate_user(username, password)
                if user_id:
                    st.session_state.authenticated = True
                    st.session_state.user_id = user_id
                    st.session_state.username = username
                    st.session_state.history = []
                    st.rerun()
                else:
                    st.error("Invalid username or password.")
        st.stop()

    # ---------- Logged-in UI ----------
    st.sidebar.title(f"👤 {st.session_state.username}")
    if st.sidebar.button("Logout"):
        st.session_state.authenticated = False
        st.session_state.user_id = None
        st.session_state.username = None
        st.session_state.history = []
        st.session_state.show_history = False
        st.rerun()

    today_scans = get_scan_count_today(st.session_state.user_id)
    st.sidebar.info(f"📊 Today's scans: {today_scans} / unlimited")

    if st.sidebar.button("📋 Toggle History"):
        st.session_state.show_history = not st.session_state.show_history
        st.rerun()

    st.title("🧾 AI Receipt Item Scanner")
    st.markdown(f"Upload up to **{MAX_FILES_PER_SUBMISSION}** receipts at a time (JPG, PNG, PDF).")

    # ---------- Uploader ----------
    uploaded_files = st.file_uploader(
        f"Choose up to {MAX_FILES_PER_SUBMISSION} receipts",
        type=["jpg", "jpeg", "png", "pdf"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_key}"
    )

    limit_exceeded = uploaded_files and len(uploaded_files) > MAX_FILES_PER_SUBMISSION
    if limit_exceeded:
        st.error(f"⚠️ You can upload a maximum of **{MAX_FILES_PER_SUBMISSION}** files. You selected **{len(uploaded_files)}**.")
        if st.button("🔄 Clear selection"):
            st.session_state.uploader_key += 1
            st.rerun()
        process_clicked = False
    else:
        col1, col2 = st.columns([1, 4])
        with col1:
            process_clicked = st.button("🚀 Process All", type="primary",
                                        disabled=(not uploaded_files))

    # ---------- Process files ----------
    if process_clicked and uploaded_files:
        if today_scans >= MAX_SCANS_PER_DAY:
            st.error(f"Daily scan limit ({MAX_SCANS_PER_DAY}) reached.")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            errors = []
            new_results = []

            for idx, file in enumerate(uploaded_files):
                status_text.text(f"Processing {file.name}... ({idx+1}/{len(uploaded_files)})")
                try:
                    result = process_single_file(file, st.session_state.user_id)
                    scan_id = save_scan(st.session_state.user_id, result)
                    result["db_id"] = scan_id
                    new_results.append(result)
                    increment_scan_count(st.session_state.user_id)
                except Exception as e:
                    errors.append(f"{file.name}: {str(e)}")
                progress_bar.progress((idx + 1) / len(uploaded_files))

            status_text.text("✅ All files processed!")
            if errors:
                st.warning("Some files had errors:")
                for err in errors:
                    st.write(f"- {err}")

            if new_results:
                st.session_state.history.extend(new_results)
                st.success(f"✅ {len(new_results)} receipt(s) successfully scanned and saved.")

            st.session_state.show_history = True
            st.rerun()

    # ---------- Display history with formatted numbers ----------
    if st.session_state.show_history and st.session_state.history:
        st.divider()
        st.subheader("📋 Scan History (Current Session)")
        st.caption("Click the '👁️' button to preview a receipt, or '🗑️' to delete. Click the table headers to sort.")

        # Build summary DataFrame with formatted numbers (as strings for display)
        summary_data = []
        for entry in st.session_state.history:
            tip_display = format_number(entry.get("tip"), as_currency=True)
            if entry.get("tip_percentage") is not None:
                tip_display += f" ({format_number(entry['tip_percentage'])}%)"
            summary_data.append({
                "File": entry["filename"][:40] + "..." if len(entry["filename"]) > 40 else entry["filename"],
                "Items": len(entry.get("items") or []),
                "Subtotal": format_number(entry.get("subtotal"), as_currency=True),
                "Tax": format_number(entry.get("tax"), as_currency=True),
                "Tip": tip_display,
                "Tip %": format_number(entry.get("tip_percentage")) + "%" if entry.get("tip_percentage") is not None else "N/A",
                "Total": format_number(entry.get("total"), as_currency=True),
                "Timestamp": entry["timestamp"]
            })
        summary_df = pd.DataFrame(summary_data)

        # Custom CSS for sticky header, background, font
        st.markdown("""
        <style>
        div[data-testid="stDataFrame"] th {
            position: sticky !important;
            top: 0px !important;
            background-color: #2a2a3e !important;
            color: white !important;
            font-size: 18px !important;
            font-weight: bold !important;
            z-index: 999 !important;
            border-bottom: 2px solid #6a6a8e !important;
        }
        </style>
        """, unsafe_allow_html=True)

        st.dataframe(summary_df, use_container_width=True)

        # Preview and delete buttons (below table)
        st.markdown("---")
        st.subheader("📄 Receipt Details")
        st.caption("Select a receipt below to preview and edit.")

        for idx, entry in enumerate(st.session_state.history):
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                st.write(f"**{entry['filename']}**")
            with col2:
                if st.button("👁️ Preview", key=f"preview_btn_{idx}"):
                    st.session_state.expanded_rows[idx] = not st.session_state.expanded_rows.get(idx, False)
                    st.rerun()
            with col3:
                if st.button("🗑️ Delete", key=f"del_btn_{idx}"):
                    if "db_id" in entry:
                        delete_scan(entry["db_id"], st.session_state.user_id)
                    st.session_state.history.pop(idx)
                    if idx in st.session_state.expanded_rows:
                        del st.session_state.expanded_rows[idx]
                    st.rerun()

            # Expander for details – with formatted metrics
            with st.expander(f"📄 {entry['filename']} – {entry['timestamp']}", expanded=st.session_state.expanded_rows.get(idx, False)):
                cols_met = st.columns(5)
                cols_met[0].metric("Subtotal", format_number(entry.get("subtotal"), as_currency=True))
                cols_met[1].metric("Tax", format_number(entry.get("tax"), as_currency=True))
                if entry.get("tip") is not None:
                    tip_label = format_number(entry.get("tip"), as_currency=True)
                    if entry.get("tip_percentage") is not None:
                        tip_label += f" ({format_number(entry['tip_percentage'])}%)"
                    cols_met[2].metric("💵 Tip", tip_label)
                else:
                    cols_met[2].metric("💵 Tip", "None")
                cols_met[3].metric("Total (final)", format_number(entry.get("total"), as_currency=True))

                if "image_bytes" in entry and entry["image_bytes"]:
                    default_width = st.session_state.img_width.get(idx, 900)
                    new_width = st.slider(
                        "🔍 Preview size",
                        min_value=100,
                        max_value=900,
                        value=default_width,
                        step=50,
                        key=f"width_slider_{idx}"
                    )
                    st.session_state.img_width[idx] = new_width
                    st.image(entry["image_bytes"], caption="Receipt preview", width=new_width)
                else:
                    st.warning("No preview image available.")

                edit_key = f"edit_{idx}"
                if st.button("✏️ Edit this receipt", key=edit_key):
                    st.session_state.edit_mode[idx] = not st.session_state.edit_mode.get(idx, False)
                    st.rerun()

                if st.session_state.edit_mode.get(idx, False):
                    st.markdown("**Edit items and totals below. Click 'Save' to confirm.**")
                    items_df = pd.DataFrame(entry["items"]) if entry["items"] else pd.DataFrame(columns=["name", "price"])
                    edited_df = st.data_editor(
                        items_df,
                        num_rows="dynamic",
                        width='stretch',
                        key=f"editor_{idx}"
                    )
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        new_subtotal = st.number_input("Subtotal", value=float(entry["subtotal"]) if entry.get("subtotal") else 0.0, step=0.01, key=f"sub_{idx}")
                    with col2:
                        new_tax = st.number_input("Tax", value=float(entry["tax"]) if entry.get("tax") else 0.0, step=0.01, key=f"tax_{idx}")
                    with col3:
                        new_tip = st.number_input("Tip", value=float(entry["tip"]) if entry.get("tip") else 0.0, step=0.01, key=f"tip_{idx}")
                    with col4:
                        new_tip_perc = st.number_input("Tip %", value=float(entry["tip_percentage"]) if entry.get("tip_percentage") else 0.0, step=0.01, key=f"tipp_{idx}")
                    new_total = st.number_input("Total (final)", value=float(entry["total"]) if entry.get("total") else 0.0, step=0.01, key=f"tot_{idx}")

                    save_col1, save_col2 = st.columns(2)
                    with save_col1:
                        if st.button("💾 Save changes", key=f"save_{idx}"):
                            st.session_state.history[idx]["items"] = edited_df.to_dict(orient="records")
                            st.session_state.history[idx]["subtotal"] = new_subtotal
                            st.session_state.history[idx]["tax"] = new_tax
                            st.session_state.history[idx]["tip"] = new_tip
                            st.session_state.history[idx]["tip_percentage"] = new_tip_perc
                            st.session_state.history[idx]["total"] = new_total
                            st.session_state.history[idx]["raw_json"]["items"] = edited_df.to_dict(orient="records")
                            st.session_state.history[idx]["raw_json"]["subtotal"] = new_subtotal
                            st.session_state.history[idx]["raw_json"]["tax"] = new_tax
                            st.session_state.history[idx]["raw_json"]["tip"] = new_tip
                            st.session_state.history[idx]["raw_json"]["tip_percentage"] = new_tip_perc
                            st.session_state.history[idx]["raw_json"]["total"] = new_total
                            if "db_id" in st.session_state.history[idx]:
                                update_scan(st.session_state.history[idx]["db_id"], st.session_state.history[idx])
                            st.success("Changes saved to database!")
                            st.session_state.edit_mode[idx] = False
                            st.rerun()
                    with save_col2:
                        if st.button("❌ Cancel", key=f"cancel_{idx}"):
                            st.session_state.edit_mode[idx] = False
                            st.rerun()
                else:
                    if entry["items"]:
                        df = pd.DataFrame(entry["items"])
                        st.dataframe(df, width='stretch')
                        csv_content = generate_detailed_csv(entry)
                        st.download_button(
                            label="⬇️ Download CSV (items + totals + tip)",
                            data=csv_content.encode('utf-8'),
                            file_name=f"{entry['filename']}_detailed.csv",
                            mime="text/csv",
                            key=f"csv_{idx}"
                        )
                    else:
                        st.warning("No items found.")

                    with st.expander("Show raw JSON"):
                        st.json(entry["raw_json"])

        # Summary CSV download (with formatted numbers)
        if st.session_state.history:
            summary_data_csv = []
            for entry in st.session_state.history:
                summary_data_csv.append({
                    "File": entry["filename"],
                    "Items": len(entry.get("items") or []),
                    "Subtotal": format_number(entry.get("subtotal"), as_currency=False),
                    "Tax": format_number(entry.get("tax"), as_currency=False),
                    "Tip": format_number(entry.get("tip"), as_currency=False),
                    "Tip %": format_number(entry.get("tip_percentage"), as_currency=False),
                    "Total": format_number(entry.get("total"), as_currency=False),
                    "Timestamp": entry["timestamp"]
                })
            csv_summary = pd.DataFrame(summary_data_csv).to_csv(index=False).encode('utf-8')
            st.download_button(
                label="⬇️ Download receipt summary (totals + tip) as CSV",
                data=csv_summary,
                file_name=f"receipt_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="csv_summary"
            )

    elif st.session_state.show_history and not st.session_state.history:
        st.info("No scans in this session. Process some receipts to see them here.")

    if not st.session_state.show_history:
        st.info("Click '📋 Toggle History' in the sidebar to view current session's scans.")

    st.divider()
    st.caption("🔒 All processing is done locally – your images are only sent to Groq for analysis.")

if __name__ == "__main__":
    main()