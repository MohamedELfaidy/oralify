# Oralify v2 - Multi-User Oral Exam Platform

## Quick Start

### 1. Install dependencies

```bash
cd oralify2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create `.env`

```
GROQ_API_KEY=your_key_here
SECRET_KEY=any-random-string
```

### 3. Run

```bash
flask run
# or
python app.py
```

Open http://127.0.0.1:5000

---

## Default Accounts

### Teacher (auto-created on first run)

- **Username:** `admin`
- **Password:** `admin123`

Change these immediately after first login via the teacher dashboard.

### Students

Students self-register at `/student/login` → "Register" tab.

---

## Pages

| URL                  | Description                             |
| -------------------- | --------------------------------------- |
| `/`                  | Home — choose Teacher or Student portal |
| `/teacher/login`     | Teacher sign-in                         |
| `/teacher/dashboard` | Teacher exam management                 |
| `/student/login`     | Student sign-in / register              |
| `/student/dashboard` | Student exam list & history             |
| `/student/exam/<id>` | Live exam session                       |

---

### Teacher flow

1. Sign in → Dashboard
2. **Create Exam** — set name, course, schedule, questions per student, allowed helping methods & costs
3. **Import Questions** — upload PDF/DOCX/image or paste text → AI generates MCQs
4. Review & edit questions (supports single and multiple correct answers)
5. Activate exam → students can attend during the scheduled window
6. View Results (password-protected blur) → export to Excel → override scores if needed

### Student flow

1. Register with college Student ID, name, email, password
2. Sign in → see available exams with status (upcoming / open / ended)
3. Click **Start Exam** → answer questions with timer
4. Use allowed helping methods (deduct marks)
5. After answering, professor confirms/overrides score → saved to database
6. View all past results in **My Results**

---

## Scoring Rules

| Situation                           | Score                         |
| ----------------------------------- | ----------------------------- |
| Single answer - correct             | Keep score                    |
| Single answer - wrong               | 0                             |
| Multi answer - both correct         | Keep score                    |
| Multi answer - one right, one wrong | −1                            |
| Multi answer - both wrong           | −2                            |
| Timer ran out                       | Fixed at 7                    |
| Help used                           | −cost (configurable per exam) |

---