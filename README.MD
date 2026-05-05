# Oralify – Oral Exam Assistant

## Getting Your Groq API Key

1. Go to **https://console.groq.com**
2. Sign up for a free account (or log in)
3. Click **"API Keys"** in the left sidebar
4. Click **"Create API Key"**, give it a name, copy the key

## Setup

### 1. Create a `.env` file in the `oralify/` folder:
```
GROQ_API_KEY=your_key_here
SECRET_KEY=any-random-string-you-choose
```

### 2. Install dependencies:
```bash
pip install -r requirements.txt
```
> **Note:** EasyOCR will download its model (~100 MB) on first use.

### 3. Run the app:
```bash
flask run
```
Then open **http://127.0.0.1:5000** in your browser.

---

## Usage

### Professor tab
- Upload a PDF / DOCX / image **or** paste text → click **"Process with AI"**
- Review & edit generated questions, then click **"Save to Database"**
- Use **"Adjust Live Score"** (password-hidden) to change a student's score mid-exam

### Student tab
- Student enters their name → exam begins
- 60-second timer per question; helping methods each cost marks and are **disabled after use**
- After all questions (or clicking Finish), the exam summary shows:
  - Final score
  - Which helping methods were used and their costs
- **Professor** types an adjusted score (hidden) or leaves blank to keep shown score → **"Confirm & Save"**

---

## File Structure
```
oralify/
├── app.py                 # Flask routes
├── models.py              # SQLAlchemy models
├── question_processor.py  # Groq API + file extraction
├── exam_logic.py          # Session & scoring logic
├── templates/
│   └── index.html         # Single-page frontend
├── requirements.txt
├── .env                   # Your API key (create this)
└── instance/
    └── exam.db            # SQLite database (auto-created)
```