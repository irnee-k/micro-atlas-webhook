import os
import psycopg2 # For connecting to PostgreSQL/Supabase
import json
import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import openai # For OpenAI API calls

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# --- Supabase Database Credentials ---
DB_HOST = os.getenv("SUPABASE_DB_HOST")
DB_PORT = os.getenv("SUPABASE_DB_PORT")
DB_NAME = os.getenv("SUPABASE_DB_NAME")
DB_USER = os.getenv("SUPABASE_DB_USER")
DB_PASSWORD = os.getenv("SUPABASE_DB_PASSWORD")

# --- OpenAI API Key ---
# Ensure this matches the key you put in your .env file
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    print("ERROR: OpenAI API key not found in environment variables for webhook_receiver.")
    # In a real production app, you might want to exit or log more severely
    # For now, let it continue but AI calls will fail.

# --- Database Connection Function ---
def get_supabase_connection():
    """Establishes and returns a connection to the Supabase PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        return conn
    except Exception as e:
        print(f"ERROR: Could not connect to Supabase: {e}")
        return None

# --- Function to Save Note to Supabase (Unified for all inputs) ---
# IMPORTANT: 'username' parameter added here
def save_note_to_database(content, summary, sentiment, keywords, username):
    """Saves processed note data to the Supabase database."""
    conn = get_supabase_connection()
    if conn is None:
        return False

    try:
        cur = conn.cursor()

        # Convert Python list of keywords to a PostgreSQL array literal string
        # This handles keywords with spaces or internal quotes correctly
        keywords_pg_array = '{' + ','.join([
            f'"{k.replace("\"", "\\\"")}"' for k in keywords
        ]) + '}'

        # IMPORTANT: 'username' and 'timestamp' columns included in the INSERT query
        insert_query = """
        INSERT INTO user_notes (content, summary, sentiment, keywords, username, timestamp)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        RETURNING id;
        """
        # IMPORTANT: 'username' value passed as a parameter here
        cur.execute(insert_query, (content, summary, sentiment, keywords_pg_array, username))
        inserted_id = cur.fetchone()[0]
        conn.commit()
        print(f"--- Note successfully saved to Supabase with ID: {inserted_id} for user: {username} ---")
        return True
    except psycopg2.Error as db_error: # Catch specific database errors for better debugging
        conn.rollback()
        print(f"DATABASE ERROR during save_note_to_database: {db_error.pgcode} - {db_error.pgerror}")
        return False
    except Exception as e: # Catch any other Python errors
        conn.rollback()
        print(f"OTHER ERROR during save_note_to_database: {e}")
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

# --- AI Analysis Functions (Using OpenAI) ---
def get_ai_analysis(text_input, prompt_type):
    """
    Generalized function to call OpenAI for various analysis tasks.
    `prompt_type` can be 'summary', 'sentiment', 'keywords', or 'full_analysis_prompt'.
    """
    system_message = "You are a helpful AI assistant."
    user_prompt = ""

    if prompt_type == 'summary':
        user_prompt = f"Summarize the following text concisely:\n\n{text_input}"
    elif prompt_type == 'sentiment':
        user_prompt = f"What is the sentiment of the following text (positive, negative, neutral)? Just provide the sentiment word.\n\n{text_input}"
    elif prompt_type == 'keywords':
        user_prompt = f"Extract 5-10 key keywords from the following text, separated by commas. Only provide the keywords.\n\n{text_input}"
    elif prompt_type == 'full_analysis_prompt': # This is if you want to use the detailed prompt from app.py
         user_prompt = f"""
You are an expert knowledge curator and cognitive cartographer, helping individuals map their learning journey.
Your task is to analyze the following unstructured text, which describes a user's recent learning, consumption, or project experiences.
From this text, you need to extract and categorize the following key elements of their knowledge landscape:

1.  **Core Concepts & Topics:** Identify the main subject matters or abstract ideas discussed.
2.  **Key Skills & Technologies:** List any specific practical abilities or tools (e.g., programming languages, software, methodologies) mentioned or clearly implied as being used or learned.
3.  **Cross-Cutting Competencies:** Identify broader, transferable skills demonstrated (e.g., Problem Solving, Data Analysis, Communication, Project Management, Critical Thinking, Leadership, User Research).
4.  **Noteworthy Connections & Insights:** Describe any explicit or implicit relationships you find between the concepts, skills, or competencies. This is where you connect disparate pieces of learning.

**Instructions for Formatting the Output:**
- Use clear, concise language.
- Format each section with a bold heading.
- Use bullet points for each item within a section.
- For "Core Concepts & Topics," provide a very brief, 1-sentence explanation if necessary.
- For "Noteworthy Connections & Insights," explain *how* different elements are related.

---
User's Learning Content to Analyze:
"{text_input}"
---
"""


    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo", # Use the model you prefer
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error during OpenAI API call for '{prompt_type}': {e}")
        return f"Error: {str(e)}"

# --- Helper to parse keywords from AI response (if needed) ---
def parse_keywords_response(response_text):
    # If the AI gives "keyword1, keyword2, keyword3"
    keywords = [k.strip() for k in response_text.split(',') if k.strip()]
    return keywords

# --- Webhook Routes (Modified to include AI analysis and Supabase save) ---

@app.route("/sms", methods=['POST'])
def sms_webhook():
    sender_number = request.form.get('From', 'Unknown')
    message_body = request.form.get('Body', '')
    print(f"\n--- New SMS Received from {sender_number} ---")
    print(f"Body: {message_body[:100]}...")

    if not message_body.strip():
        print("Empty SMS body received.")
        return jsonify({"status": "error", "message": "Empty SMS body"}), 400

    # Perform AI analysis
    summary = get_ai_analysis(message_body, 'summary')
    sentiment = get_ai_analysis(message_body, 'sentiment')
    raw_keywords_response = get_ai_analysis(message_body, 'keywords')
    keywords = parse_keywords_response(raw_keywords_response)

    # Use the sender_number as the username for this note
    username_for_note = sender_number

    # IMPORTANT: Passed username_for_note to save_note_to_database
    if save_note_to_database(message_body, summary, sentiment, keywords, username_for_note):
        response_msg = "Your SMS note has been processed and saved to Micro-Atlas! 🧠"
        print(response_msg)
        twilio_response = MessagingResponse()
        twilio_response.message(response_msg)
        return str(twilio_response), 200
    else:
        print("Failed to save SMS to Supabase.")
        twilio_response = MessagingResponse()
        twilio_response.message("Failed to process your SMS note. Please try again.")
        return str(twilio_response), 500


@app.route("/web_clip", methods=['POST'])
def web_clip_webhook():
    print(f"\n--- Incoming Web Clip Request Received! ---")
    if not request.is_json:
        print("ERROR: Web clip request did not contain JSON data.")
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    clipped_url = data.get('url')
    clipped_text = data.get('text')
    username_for_note = data.get('username') # IMPORTANT: Extract username from JSON payload

    # Optional: Add robustness if username might be missing from payload
    if not username_for_note:
        print("WARNING: 'username' not provided in web clip data. Using 'unknown_web_clipper' as default.")
        username_for_note = 'unknown_web_clipper'


    if not clipped_url or not clipped_text.strip():
        print("ERROR: Missing 'url' or 'text' in web clip data.")
        return jsonify({"error": "Missing 'url' or 'text' in request body"}), 400

    full_content = f"Web Clip from {clipped_url}:\n\n{clipped_text}"
    print(f"Clipped URL: {clipped_url}")
    print(f"Clipped Text (first 100 chars): {clipped_text[:100]}...")

    # Perform AI analysis
    summary = get_ai_analysis(full_content, 'summary')
    sentiment = get_ai_analysis(full_content, 'sentiment')
    raw_keywords_response = get_ai_analysis(full_content, 'keywords')
    keywords = parse_keywords_response(raw_keywords_response)

    # IMPORTANT: Passed username_for_note to save_note_to_database
    if save_note_to_database(full_content, summary, sentiment, keywords, username_for_note):
        print("Web clip successfully processed and saved to Supabase.")
        return jsonify({"message": "Web clip received and saved!"}), 200
    else:
        print("Failed to save web clip to Supabase.")
        return jsonify({"error": "Failed to save web clip"}), 500


@app.route('/email_inbound', methods=['POST'])
def receive_email():
    print(f"\n--- New Email Received ---")
    # For Mailgun (common fields):
    sender = request.form.get('sender')
    subject = request.form.get('subject')
    body_plain = request.form.get('body-plain')

    if not body_plain.strip():
        print("ERROR: Received email without plain text body.")
        return "Missing email body", 400

    # Combine subject and body for AI analysis and storage
    full_content = f"Subject: {subject}\n\n{body_plain}" if subject else body_plain

    print(f"From: {sender}")
    print(f"Subject: {subject}")
    print(f"Body (first 100 chars): {body_plain[:100]}...")

    # Perform AI analysis
    summary = get_ai_analysis(full_content, 'summary')
    sentiment = get_ai_analysis(full_content, 'sentiment')
    raw_keywords_response = get_ai_analysis(full_content, 'keywords')
    keywords = parse_keywords_response(raw_keywords_response)

    # IMPORTANT: Passed sender to save_note_to_database
    if save_note_to_database(full_content, summary, sentiment, keywords, sender):
        print("Email successfully processed and saved to Supabase.")
        return "Email received and saved!", 200
    else:
        print("Failed to save email to Supabase.")
        return "Error saving email", 500

# This block allows us to run the server directly from the command line
if __name__ == "__main__":
    print("Starting Flask server on http://localhost:5001")
    app.run(port=5001, debug=True)
