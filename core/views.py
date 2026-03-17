from django.shortcuts import render, redirect, get_object_or_404
from django.db import models, DatabaseError
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from django.http import JsonResponse, HttpResponse, Http404
from datetime import date, datetime, timedelta, timezone as dt_timezone
import re
import csv
import urllib.parse
import urllib.request
import urllib.error
import uuid

from .models import (
    Profile,
    ChatThread,
    ChatMessage,
    ChatThreadMute,
    ChatThreadReadState,
    ChatTypingState,
    Meeting,
    Task,
    TaskComment,
    TaskReminderLog,
    Submission,
    HolidayRequest,
    Notification,
    GoogleCalendarConnection,
    BotReminder,
    BotMessage,
)
import json

ANNUAL_LEAVE_DEFAULT_DAYS = 28


BOT_FAQ = [
    {
        'topic': 'tasks',
        'keys': ['task', 'tasks', 'taskuri', 'sarcina', 'sarcini', 'todo', 'to do', 'in revision', 'done', 'archived'],
        'short': "Pentru taskuri: mergi in `Tasks`, creezi task, setezi status/priority si assignee.",
        'long': (
            "Pasii pe scurt: `Tasks` -> completezi titlu/descriere -> alegi assignee, status, priority -> salvezi. "
            "Taskurile pot fi mutate intre `To Do`, `In Revision`, `Done`, `Archived`."
        ),
    },
    {
        'topic': 'meetings',
        'keys': [
            'meeting', 'meetings', 'meeting-uri', 'meet', 'intalnire', 'intalniri',
            'call', 'calendar', 'calend', 'program', 'programare', 'sync', 'google'
        ],
        'short': "Meeting-uri: `Dashboard` -> `Add meeting`. Pentru sync Google trebuie conectat contul.",
        'long': (
            "Meeting-urile se salveaza local si, daca ai Google conectat, se sincronizeaza in calendarul tau `primary`. "
            "Daca nu apare in Google, verifica daca esti conectat si daca folosesti acelasi user."
        ),
    },
    {
        'topic': 'chat',
        'keys': ['chat', 'mesaj', 'mesaje', 'dm', 'team chat', 'scriu', 'scrie', 'conversatie'],
        'short': "Chat-ul e in `Chat`. Poti trimite mesaje, fisiere si mute pe thread-uri.",
        'long': (
            "In `Chat` ai thread-uri DM si grup. Click pe thread, scrii mesaj, poti atasa fisier. "
            "Ai si mute pe thread daca nu vrei notificari."
        ),
    },
    {
        'topic': 'notifications',
        'keys': ['notific', 'notification', 'notif', 'clopotel', 'alerte'],
        'short': "Notificarile sunt in bara de sus. Le poti marca `Mark all as read`.",
        'long': (
            "Notificarile apar in clopotel. Poti marca individual sau `Mark all as read`. "
            "Dupa refresh, cele citite nu mai apar."
        ),
    },
    {
        'topic': 'holidays',
        'keys': ['concediu', 'holiday', 'leave', 'vacanta', 'zile libere'],
        'short': "Concediile sunt in `Calendar` -> `Requests`. Adminul poate aproba/respinge.",
        'long': (
            "Din `Calendar` -> `Requests` poti crea cerere de concediu. Adminul vede si aproba/respinge. "
            "Ai si statusurile `Pending/Approved/Rejected`."
        ),
    },
    {
        'topic': 'submissions',
        'keys': ['submission', 'submit', 'review', 'rejected', 'approved', 'predare', 'trimis', 'upload'],
        'short': "Submissions: intra pe task si trimite submission. Adminul le aproba/respinge.",
        'long': (
            "Deschizi task -> `Submit` -> optional fisier si descriere. Adminul vede submission-ul si poate aproba/respinge."
        ),
    },
    {
        'topic': 'csv',
        'keys': ['export', 'csv'],
        'short': "Export CSV este disponibil la taskuri si la cererile de concediu.",
        'long': (
            "Ai export pentru: lista de taskuri, task individual si cereri de concediu. Fisierele se descarca direct."
        ),
    },
    {
        'topic': 'profile',
        'keys': ['profil', 'profile', 'avatar', 'username'],
        'short': "Profilul e in `Profile` unde poti actualiza datele si avatarul.",
        'long': (
            "In `Profile` poti edita nume, avatar, info personale si detalii de experienta."
        ),
    },
]


_ROMANIAN_MAP = str.maketrans({
    'ă': 'a', 'â': 'a', 'î': 'i', 'ș': 's', 'ş': 's', 'ț': 't', 'ţ': 't',
    'Ă': 'a', 'Â': 'a', 'Î': 'i', 'Ș': 's', 'Ş': 's', 'Ț': 't', 'Ţ': 't',
})


def _normalize_text(text):
    text = (text or '').strip().lower()
    text = text.translate(_ROMANIAN_MAP)
    text = re.sub(r'[^a-z0-9\\s]', ' ', text)
    text = re.sub(r'\\s+', ' ', text).strip()
    return text


def _keyword_hit(query, tokens, keyword):
    if not keyword:
        return False
    if keyword in query:
        return True
    for tok in tokens:
        if tok.startswith(keyword) or keyword.startswith(tok):
            return True
    return False


def _find_topic(query):
    tokens = query.split()
    best = None
    best_score = 0
    for item in BOT_FAQ:
        score = 0
        for raw in item['keys']:
            k = _normalize_text(raw)
            if _keyword_hit(query, tokens, k):
                score += 1
        if score > best_score:
            best_score = score
            best = item
    return best
def _openai_config():
    key = str(getattr(settings, 'OPENAI_API_KEY', '') or '').strip()
    model = str(getattr(settings, 'OPENAI_MODEL', '') or 'gpt-4o-mini').strip()
    base = str(getattr(settings, 'OPENAI_API_BASE', '') or 'https://api.openai.com/v1').strip()
    if base.endswith('/'):
        base = base[:-1]
    return key, model, base


def _openai_ready():
    enabled = str(getattr(settings, 'OPENAI_ENABLED', '') or '').strip().lower()
    if enabled not in {'1', 'true', 'yes', 'on'}:
        return False
    key, _, _ = _openai_config()
    return bool(key)


def _openai_extract_text(payload):
    if not payload:
        return ''
    # Responses API format
    output = payload.get('output') or []
    for item in output:
        if item.get('type') != 'message':
            continue
        content = item.get('content') or []
        for c in content:
            if c.get('type') == 'output_text' and c.get('text'):
                return c.get('text')
    # Fallbacks
    if payload.get('output_text'):
        return payload.get('output_text')
    return ''


def _openai_request(prompt, instructions=None):
    key, model, base = _openai_config()
    if not key:
        return None, 'missing_api_key'
    payload = {
        'model': model,
        'input': prompt,
        'max_output_tokens': 400,
        'temperature': 0.3,
    }
    if instructions:
        payload['instructions'] = instructions
    req = urllib.request.Request(
        f'{base}/responses',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode('utf-8') or '{}'
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode('utf-8', errors='ignore')
        return None, f'http_error_{exc.code}: {err_body[:200]}'
    except Exception as exc:
        return None, f'error: {exc}'
    try:
        return json.loads(raw), None
    except Exception:
        return None, 'invalid_json'


def _bot_context_summary(user):
    if not user or not user.is_authenticated:
        return "User: not authenticated."
    parts = []
    name = user.get_full_name() or user.username
    parts.append(f"User: {name} (@{user.username}).")

    task_qs = Task.objects.filter(assignee=user)
    total_tasks = task_qs.count()
    if total_tasks:
        todo = task_qs.filter(status='todo').count()
        revision = task_qs.filter(status='revision').count()
        done = task_qs.filter(status='done').count()
        archived = task_qs.filter(status='archived').count()
        parts.append(f"Tasks: total {total_tasks} (todo {todo}, revision {revision}, done {done}, archived {archived}).")
        items = task_qs.order_by('due_date', '-created_at')[:3]
        parts.append("Top tasks: " + "; ".join([
            f"{t.title} ({t.get_status_display()}, {t.due_date.strftime('%d.%m.%Y') if t.due_date else 'no due'})"
            for t in items
        ]))
    else:
        parts.append("Tasks: none assigned.")

    today = timezone.localdate()
    meetings = Meeting.objects.filter(created_by=user, date__gte=today).order_by('date', 'time')[:3]
    if meetings:
        parts.append("Upcoming meetings: " + "; ".join([
            f"{m.title} on {m.date.strftime('%d.%m.%Y')} at {m.time.strftime('%H:%M') if m.time else 'no time'}"
            for m in meetings
        ]))
    else:
        parts.append("Upcoming meetings: none.")

    unread = Notification.objects.filter(user=user, is_read=False).count()
    parts.append(f"Notifications unread: {unread}.")

    balance = _leave_balance_for_user(user)
    parts.append(
        f"Leave balance: allocated {balance['allocated']}, remaining {balance['remaining']}, pending {balance['pending']}."
    )

    return " ".join(parts)


def _bot_dynamic_answer(topic, request, detailed=False):
    if not request or not getattr(request, 'user', None) or not request.user.is_authenticated:
        return None
    user = request.user

    if topic == 'tasks':
        qs = Task.objects.filter(assignee=user)
        total = qs.count()
        if total == 0:
            return "Nu ai taskuri asignate momentan."
        todo = qs.filter(status='todo').count()
        revision = qs.filter(status='revision').count()
        done = qs.filter(status='done').count()
        archived = qs.filter(status='archived').count()
        if not detailed:
            return f"Ai {total} taskuri: To Do {todo}, In Revision {revision}, Done {done}, Archived {archived}."
        items = qs.order_by('due_date', '-created_at')[:3]
        lines = []
        for t in items:
            due = t.due_date.strftime('%d.%m.%Y') if t.due_date else 'fara termen'
            lines.append(f"{t.title} ({t.get_status_display()}, {due})")
        return "Iata primele taskuri:\n" + _bot_structured_lines(lines)

    if topic == 'meetings':
        today = timezone.localdate()
        qs = Meeting.objects.filter(created_by=user, date__gte=today).order_by('date', 'time')
        if not qs.exists():
            return "Nu ai meeting-uri viitoare."
        if not detailed:
            m = qs.first()
            time_txt = m.time.strftime('%H:%M') if m.time else 'fara ora'
            return f"Urmatorul meeting: {m.title} pe {m.date.strftime('%d.%m.%Y')} la {time_txt}."
        items = qs[:3]
        lines = []
        for m in items:
            time_txt = m.time.strftime('%H:%M') if m.time else 'fara ora'
            lines.append(f"{m.title} pe {m.date.strftime('%d.%m.%Y')} la {time_txt}")
        return "Urmatoarele meeting-uri:\n" + _bot_structured_lines(lines)

    if topic == 'notifications':
        unread = Notification.objects.filter(user=user, is_read=False).count()
        return f"Ai {unread} notificari necitite."

    if topic == 'holidays':
        balance = _leave_balance_for_user(user)
        pending = HolidayRequest.objects.filter(user=user, status='pending').count()
        return (
            f"Concediu anual: alocate {balance['allocated']}, ramase {balance['remaining']}, "
            f"cereri in asteptare {pending}."
        )

    if topic == 'submissions':
        if _is_admin_user(user):
            pending = Submission.objects.filter(status='pending').count()
            return f"Submissions in asteptare (admin): {pending}."
        my_pending = Submission.objects.filter(author=user, status='pending').count()
        my_approved = Submission.objects.filter(author=user, status='approved').count()
        my_rejected = Submission.objects.filter(author=user, status='rejected').count()
        return f"Submissions: pending {my_pending}, approved {my_approved}, rejected {my_rejected}."

    if topic == 'chat':
        threads = ChatThread.objects.filter(participants=user).count()
        return f"Ai acces la {threads} thread-uri in chat."

    return None


def _parse_date_text(text):
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _bot_today_summary(user):
    today = timezone.localdate()
    tasks_due = Task.objects.filter(assignee=user, due_date=today).count()
    meetings_today = Meeting.objects.filter(created_by=user, date=today).count()
    unread = Notification.objects.filter(user=user, is_read=False).count()
    return (
        f"Azi ai: taskuri cu termen {tasks_due}, meeting-uri {meetings_today}, notificari necitite {unread}."
    )


def _bot_overdue_tasks(user):
    today = timezone.localdate()
    qs = Task.objects.filter(
        assignee=user,
        due_date__lt=today,
    ).exclude(status__in=['done', 'archived']).order_by('due_date')[:5]
    if not qs.exists():
        return "Nu ai taskuri intarziate."
    lines = []
    for t in qs:
        due = t.due_date.strftime('%d.%m.%Y') if t.due_date else 'fara termen'
        lines.append(f"{t.title} (termen {due})")
    return "Taskuri intarziate:\n" + _bot_structured_lines(lines)


def _bot_high_priority_tasks(user):
    qs = Task.objects.filter(assignee=user, priority='high').exclude(status='archived').order_by('due_date')[:5]
    if not qs.exists():
        return "Nu ai taskuri cu prioritate mare."
    lines = []
    for t in qs:
        due = t.due_date.strftime('%d.%m.%Y') if t.due_date else 'fara termen'
        lines.append(f"{t.title} (termen {due})")
    return "Taskuri cu prioritate mare:\n" + _bot_structured_lines(lines)


def _bot_people_on_leave(user):
    if not _is_admin_user(user):
        return "Nu am permisiuni sa afisez lista completa a concediilor."
    today = timezone.localdate()
    qs = HolidayRequest.objects.filter(
        status='approved',
        start_date__lte=today,
        end_date__gte=today,
    ).select_related('user', 'user__profile')[:10]
    if not qs.exists():
        return "Nu este nimeni in concediu acum."
    names = []
    for r in qs:
        prof = getattr(r.user, 'profile', None)
        display = (getattr(prof, 'full_name', '') or r.user.get_full_name() or r.user.username)
        names.append(display)
    return "In concediu acum: " + ", ".join(names)


def _bot_latest_submissions(user):
    if not _is_admin_user(user):
        return None
    qs = Submission.objects.order_by('-created_at')[:5]
    if not qs.exists():
        return "Nu exista submissions."
    lines = []
    for s in qs:
        lines.append(f"{s.task.title} de {s.author.username} ({s.get_status_display()})")
    return "Ultimele submissions:\n" + _bot_structured_lines(lines)

def _bot_structured_lines(lines):
    # Ensure consistent bullet formatting
    return "\n".join([f"- {line}" for line in lines if line])


def _bot_suggestions_for_topic(topic):
    base = ["Rezumat azi", "Taskuri intarziate", "Prioritate mare", "Urmatorul deadline"]
    if topic == 'tasks':
        return ["Taskuri intarziate", "Prioritate mare", "Urmatorul deadline"]
    if topic == 'meetings':
        return ["Urmatorul meeting", "Cum adaug meeting?", "Google sync nu merge"]
    if topic == 'notifications':
        return ["Notificari necitite", "Rezumat azi"]
    if topic == 'holidays':
        return ["Cine e in concediu acum?", "Concediu anual ramas"]
    if topic == 'submissions':
        return ["Ultimele submissions", "Submissions pending"]
    if topic == 'chat':
        return ["Cum trimit mesaj?", "Cum dau mute?"]
    return base


def _bot_handle_task_creation(query, request):
    if not request or not request.user.is_authenticated:
        return None
    lower = query
    if any(k in lower for k in ['renunta', 'anuleaza', 'cancel']):
        request.session.pop('bot_task_draft', None)
        return "Am anulat crearea taskului."

    draft = request.session.get('bot_task_draft') or {}
    task_triggers = ['task', 'taskuri', 'sarcina', 'sarcini', 'creeaza task', 'adauga task', 'task nou', 'taskul nou']
    started = any(k in lower for k in task_triggers)
    is_confirm = any(k in lower for k in ['confirm', 'confirma'])

    # If we already have a draft but the message is not task-related, ignore the draft.
    if draft and not started and not is_confirm:
        return None

    # Detect inline title: "creeaza task <title>"
    if started and not draft.get('title'):
        m = re.search(r'(creeaza task|adauga task|task nou)\\s+(.*)$', lower)
        if m and m.group(2).strip():
            draft['title'] = m.group(2).strip()[:200]

    # Parse assignee
    if 'assignee:' in lower or 'assign:' in lower or 'user:' in lower or '@' in lower:
        uname = None
        m = re.search(r'@([a-z0-9_\\.\\-]+)', lower)
        if m:
            uname = m.group(1)
        else:
            m = re.search(r'(assignee|assign|user)\\s*:\\s*([a-z0-9_\\.\\-]+)', lower)
            if m:
                uname = m.group(2)
        if uname:
            u = User.objects.filter(username__iexact=uname).first()
            if not u:
                u = User.objects.filter(email__iexact=uname).first()
            if u:
                draft['assignee_id'] = u.id
                draft['assignee_label'] = u.username
            else:
                return f"Nu am gasit userul `{uname}`. Scrie @username corect."

    # Parse priority
    if 'prioritate' in lower or 'priority' in lower:
        if 'high' in lower or 'mare' in lower or 'urgent' in lower:
            draft['priority'] = 'high'
        elif 'medium' in lower or 'mediu' in lower:
            draft['priority'] = 'medium'
        elif 'low' in lower or 'mica' in lower:
            draft['priority'] = 'low'

    # Parse due date
    m = re.search(r'(termen|due)\\s*:\\s*([0-9]{2}[./][0-9]{2}[./][0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})', lower)
    if m:
        dt = _parse_date_text(m.group(2))
        if dt:
            draft['due_date'] = dt.isoformat()

    if started or draft:
        request.session['bot_task_draft'] = draft
        if is_confirm or lower.strip() == 'da':
            title = draft.get('title')
            if not title:
                return "Spune titlul taskului. Exemplu: `creeaza task Landing page`."
            assignee_id = draft.get('assignee_id')
            assignee = User.objects.filter(id=assignee_id).first() if assignee_id else None
            priority = draft.get('priority') or 'medium'
            due_date = None
            if draft.get('due_date'):
                try:
                    due_date = datetime.fromisoformat(draft['due_date']).date()
                except Exception:
                    due_date = None
            task = Task.objects.create(
                title=title,
                description='',
                status='todo',
                priority=priority,
                due_date=due_date,
                assignee=assignee,
                created_by=request.user,
            )
            request.session.pop('bot_task_draft', None)
            return f"Task creat: {task.title} (prioritate {task.priority})."

        missing = []
        if not draft.get('title'):
            missing.append("titlu")
        # Build guidance
        hint = "Spune: `creeaza task Titlu`"
        hint += ", optional `assignee: @username`, `prioritate: high`, `termen: 2026-03-21`."
        return "Pregatesc un task nou. Lipseste: " + ", ".join(missing) + ". " + hint if missing else (
            "Am datele. Scrie `confirm` ca sa creez taskul."
        )

    return None


def _parse_reminder_time(query):
    # "peste 2 ore" / "peste 30 min"
    m = re.search(r'peste\\s+(\\d{1,3})\\s*(minute|min|ore|ora|hours|hour|h)', query)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit in {'ore', 'ora', 'hours', 'hour', 'h'}:
            return timezone.now() + timedelta(hours=val)
        return timezone.now() + timedelta(minutes=val)

    # "in 2 zile"
    m = re.search(r'in\\s+(\\d{1,3})\\s*zile', query)
    if m:
        val = int(m.group(1))
        return timezone.now() + timedelta(days=val)

    # "la 14:30" optionally "maine"
    m = re.search(r'la\\s*(\\d{1,2}):(\\d{2})', query)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        today = timezone.localdate()
        target_date = today
        if 'maine' in query:
            target_date = today + timedelta(days=1)
        dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
        if dt <= timezone.now():
            dt = dt + timedelta(days=1)
        return dt

    return None


def _bot_handle_reminder(query, request):
    if not request or not request.user.is_authenticated:
        return None
    if not any(k in query for k in ['aminteste', 'reminder', 'remainder', 'remind']):
        return None
    # extract message after "sa" or "ca"
    msg = None
    m = re.search(r'aminteste[- ]?mi\\s+(sa\\s+)?(.+)$', query)
    if m:
        msg = m.group(2).strip()
    if not msg:
        msg = "Reminder"
    remind_at = _parse_reminder_time(query)
    if not remind_at:
        return "Spune si cand: ex. `aminteste-mi peste 2 ore` sau `aminteste-mi maine la 10:30`."
    BotReminder.objects.create(user=request.user, message=msg[:255], remind_at=remind_at)
    return f"Ok, iti reamintesc la {timezone.localtime(remind_at).strftime('%d.%m.%Y %H:%M')}."


def _bot_answer(text, request=None):
    query = _normalize_text(text)
    if not query:
        return "Scrie o intrebare scurta, de exemplu: 'Cum adaug task?'"

    # reminder flow
    reminder_flow = _bot_handle_reminder(query, request)
    if reminder_flow:
        return reminder_flow

    # task creation flow (after reminders)
    task_flow = _bot_handle_task_creation(query, request)
    if task_flow:
        return task_flow

    greet_keys = ['salut', 'buna', 'hello', 'hey', 'hi', 'servus', 'neata']
    if any(k in query.split() for k in greet_keys):
        if request and getattr(request, 'user', None) and request.user.is_authenticated:
            name = request.user.get_full_name() or request.user.username
            return f"Salut, {name}! Cu ce te pot ajuta?"
        return "Salut! Cu ce te pot ajuta?"

    thanks_keys = ['mersi', 'merci', 'multumesc', 'thanks']
    if any(k in query.split() for k in thanks_keys):
        return "Cu placere! Mai ai nevoie de ceva?"

    help_keys = ['ajutor', 'help', 'comenzi', 'ce poti', 'ce stii', 'capabilitati']
    if any(k in query for k in help_keys):
        return (
            "Pot ajuta cu: taskuri, meeting-uri, chat, notificari, concedii, submissions, export CSV, "
            "rezumat azi, taskuri intarziate, prioritate mare, concedii acum, reminder. "
            "Exemple: 'taskuri', 'meeting', 'concediu', 'rezumat azi', 'aminteste-mi peste 2 ore'."
        )

    # Summary today
    if 'rezumat' in query or 'azi' in query or 'today' in query:
        if request and request.user.is_authenticated:
            return _bot_today_summary(request.user)

    # Overdue tasks
    if 'intarziat' in query or 'overdue' in query or 'depasit' in query:
        if request and request.user.is_authenticated:
            return _bot_overdue_tasks(request.user)

    # High priority
    if 'prioritate mare' in query or 'urgent' in query or 'high' in query:
        if request and request.user.is_authenticated:
            return _bot_high_priority_tasks(request.user)

    # Who is on leave
    if 'cine' in query and ('concediu' in query or 'vacanta' in query):
        if request and request.user.is_authenticated:
            return _bot_people_on_leave(request.user)

    # Latest submissions (admin)
    if 'ultimele' in query and 'submission' in query:
        if request and request.user.is_authenticated:
            resp = _bot_latest_submissions(request.user)
            if resp:
                return resp

    # Search tasks by keyword: "cauta task <text>"
    m = re.search(r'cauta\\s+task\\s+(.+)$', query)
    if m and request and request.user.is_authenticated:
        term = m.group(1).strip()
        qs = Task.objects.filter(
            models.Q(title__icontains=term) | models.Q(description__icontains=term)
        ).order_by('due_date')[:5]
        if not qs.exists():
            return f"Nu am gasit taskuri pentru: {term}."
        lines = []
        for t in qs:
            due = t.due_date.strftime('%d.%m.%Y') if t.due_date else 'fara termen'
            lines.append(f"{t.title} ({t.get_status_display()}, {due})")
        return "Rezultate:\n" + _bot_structured_lines(lines)

    # Next deadline
    if 'urmatorul' in query and ('deadline' in query or 'termen' in query):
        if request and request.user.is_authenticated:
            today = timezone.localdate()
            t = Task.objects.filter(
                assignee=request.user,
                due_date__gte=today,
            ).exclude(status__in=['done', 'archived']).order_by('due_date').first()
            if not t:
                return "Nu ai deadline-uri viitoare."
            return f"Urmatorul deadline: {t.title} pe {t.due_date.strftime('%d.%m.%Y')}."

    # Tasks without assignee (admin)
    if 'fara assignee' in query or 'neatribuit' in query:
        if request and request.user.is_authenticated and _is_admin_user(request.user):
            qs = Task.objects.filter(assignee__isnull=True).order_by('-created_at')[:5]
            if not qs.exists():
                return "Nu exista taskuri fara assignee."
            lines = [f"- {t.title}" for t in qs]
            return "Taskuri fara assignee:\n" + "\n".join(lines)

    # troubleshooting quick path
    if ('google' in query or 'sync' in query or 'calendar' in query) and ('nu' in query or 'eroare' in query or 'problema' in query):
        return (
            "Pentru sync Google: verifica daca esti conectat la Google Calendar, "
            "daca ai credit activ si ca folosesti acelasi user care creeaza meeting-ul."
        )

    # link guidance
    if 'unde' in query or 'link' in query:
        item = _find_topic(query)
        if item:
            page_map = {
                'tasks': '/tasks/',
                'meetings': '/dashboard/',
                'notifications': '/dashboard/',
                'holidays': '/calendar/requests/',
                'submissions': '/submissions/admin/',
                'chat': '/chat/',
                'profile': '/profile/',
            }
            link = page_map.get(item['topic'])
            if link:
                return f"Poti deschide: {link}"

    follow_keys = ['detaliaza', 'detalii', 'mai multe', 'continua', 'ok', 'da', 'explica']
    last_topic = None
    if request and hasattr(request, 'session'):
        last_topic = request.session.get('bot_last_topic')
    if last_topic and any(k in query for k in follow_keys):
        dyn = _bot_dynamic_answer(last_topic, request, detailed=True)
        if dyn:
            return dyn
        for item in BOT_FAQ:
            if item['topic'] == last_topic:
                return item['long']

    item = _find_topic(query)
    if item:
        if request and hasattr(request, 'session'):
            request.session['bot_last_topic'] = item['topic']
        dyn = _bot_dynamic_answer(item['topic'], request, detailed=False)
        if dyn:
            return dyn
        return item['short']

    return (
        "Pot ajuta cu: taskuri, meeting-uri, chat, notificari, concedii, submissions, export CSV. "
        "Scrie una din aceste teme."
    )


def _ensure_profile(user):
    prof = getattr(user, 'profile', None)
    if prof is None:
        prof, _ = Profile.objects.get_or_create(user=user)
    if not getattr(prof, 'calendar_feed_token', None):
        prof.calendar_feed_token = uuid.uuid4()
        prof.save(update_fields=['calendar_feed_token'])
    return prof


def _calendar_sync_url(request, user):
    prof = _ensure_profile(user)
    return request.build_absolute_uri(reverse('calendar_feed_ics', args=[str(prof.calendar_feed_token)]))


def _ics_escape(value):
    return (
        str(value or '')
        .replace('\\', '\\\\')
        .replace(';', '\\;')
        .replace(',', '\\,')
        .replace('\r\n', '\n')
        .replace('\r', '\n')
        .replace('\n', '\\n')
    )


def _ics_dt_utc(dt):
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')


GOOGLE_OAUTH_SCOPE = 'https://www.googleapis.com/auth/calendar.events'
GOOGLE_AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
GOOGLE_CALENDAR_API_BASE = 'https://www.googleapis.com/calendar/v3'


def _google_oauth_credentials():
    client_id = str(getattr(settings, 'GOOGLE_CLIENT_ID', '') or '').strip()
    client_secret = str(getattr(settings, 'GOOGLE_CLIENT_SECRET', '') or '').strip()
    return client_id, client_secret


def _google_oauth_ready():
    client_id, client_secret = _google_oauth_credentials()
    return bool(client_id and client_secret)


def _google_redirect_uri(request):
    configured = str(getattr(settings, 'GOOGLE_REDIRECT_URI', '') or '').strip()
    if configured:
        return configured
    return request.build_absolute_uri(reverse('google_calendar_callback'))


def _google_connection(user):
    return GoogleCalendarConnection.objects.filter(user=user, sync_enabled=True).first()


def _google_calendar_connected(user):
    return _google_connection(user) is not None


def _google_needs_refresh(connection):
    if not connection.access_token or not connection.access_token_expires_at:
        return True
    return connection.access_token_expires_at <= timezone.now() + timedelta(seconds=30)


def _google_token_exchange(payload):
    req = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(payload).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode('utf-8') or '{}'
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Google token error ({exc.code}): {err_body[:200]}')
    except Exception as exc:
        raise RuntimeError(f'Google token error: {exc}')
    try:
        return json.loads(body)
    except Exception:
        return {}


def _google_refresh_access_token(connection):
    client_id, client_secret = _google_oauth_credentials()
    if not (client_id and client_secret and connection.refresh_token):
        raise RuntimeError('Google OAuth is not configured.')

    data = _google_token_exchange({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': connection.refresh_token,
        'grant_type': 'refresh_token',
    })
    access_token = (data.get('access_token') or '').strip()
    if not access_token:
        raise RuntimeError('Google did not return an access token.')

    expires_in = int(data.get('expires_in') or 3600)
    connection.access_token = access_token
    connection.access_token_expires_at = timezone.now() + timedelta(seconds=max(expires_in - 30, 60))
    connection.save(update_fields=['access_token', 'access_token_expires_at', 'updated_at'])
    return access_token


def _google_access_token_for_user(user):
    connection = _google_connection(user)
    if not connection:
        return None, None
    if _google_needs_refresh(connection):
        token = _google_refresh_access_token(connection)
        return connection, token
    return connection, connection.access_token


def _google_calendar_request(method, path, access_token, payload=None):
    url = f'{GOOGLE_CALENDAR_API_BASE}{path}'
    body = None
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
    }
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode('utf-8') or ''
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Google calendar error ({exc.code}): {err_body[:200]}')
    except Exception as exc:
        raise RuntimeError(f'Google calendar error: {exc}')

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _google_event_payload(meeting):
    tz_name = timezone.get_current_timezone_name() or 'Europe/Bucharest'
    payload = {
        'summary': meeting.title or 'Meeting',
    }
    if meeting.location:
        payload['location'] = meeting.location

    details = []
    if meeting.description:
        details.append(meeting.description)
    if meeting.participants:
        details.append(f'Participants: {meeting.participants}')
    details.append(f'Created by: {meeting.created_by.get_full_name() or meeting.created_by.username}')
    payload['description'] = '\n'.join(details)

    if meeting.time:
        start_dt = datetime.combine(meeting.date, meeting.time)
        start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
        end_dt = start_dt + timedelta(hours=1)
        payload['start'] = {'dateTime': start_dt.isoformat(), 'timeZone': tz_name}
        payload['end'] = {'dateTime': end_dt.isoformat(), 'timeZone': tz_name}
    else:
        payload['start'] = {'date': meeting.date.isoformat()}
        payload['end'] = {'date': (meeting.date + timedelta(days=1)).isoformat()}

    return payload


def _sync_meeting_to_google(meeting):
    try:
        connection, access_token = _google_access_token_for_user(meeting.created_by)
    except Exception as exc:
        return False, str(exc)
    if not connection:
        return True, 'not_connected'
    if not access_token:
        return False, 'missing_access_token'

    calendar_id = urllib.parse.quote(connection.calendar_id or 'primary', safe='')
    payload = _google_event_payload(meeting)
    try:
        if meeting.google_event_id:
            event_id = urllib.parse.quote(meeting.google_event_id, safe='')
            path = f'/calendars/{calendar_id}/events/{event_id}'
            _google_calendar_request('PATCH', path, access_token, payload=payload)
        else:
            path = f'/calendars/{calendar_id}/events'
            resp = _google_calendar_request('POST', path, access_token, payload=payload)
            event_id = (resp or {}).get('id')
            if event_id:
                Meeting.objects.filter(id=meeting.id).update(google_event_id=event_id)
                meeting.google_event_id = event_id
        return True, 'ok'
    except Exception as exc:
        return False, str(exc)


def _delete_meeting_from_google(owner_user, google_event_id):
    if not google_event_id:
        return True, 'no_event'

    try:
        connection, access_token = _google_access_token_for_user(owner_user)
    except Exception as exc:
        return False, str(exc)
    if not connection:
        return True, 'not_connected'
    if not access_token:
        return False, 'missing_access_token'

    calendar_id = urllib.parse.quote(connection.calendar_id or 'primary', safe='')
    event_id = urllib.parse.quote(google_event_id, safe='')
    path = f'/calendars/{calendar_id}/events/{event_id}'
    try:
        _google_calendar_request('DELETE', path, access_token)
        return True, 'ok'
    except Exception as exc:
        # Dacă event-ul nu mai există pe Google, local îl considerăm deja șters.
        if '404' in str(exc):
            return True, 'already_deleted'
        return False, str(exc)


def _task_reminder_type_for_date(due_date, today):
    if not due_date:
        return None
    if due_date == today + timedelta(days=1):
        return 'due_24h'
    if due_date == today:
        return 'due_today'
    if due_date < today:
        return 'overdue'
    return None


def _task_reminder_text(task, reminder_type):
    due_txt = task.due_date.strftime('%d.%m.%Y') if task.due_date else ''
    title_txt = task.title or 'Untitled task'
    if reminder_type == 'due_24h':
        return f"Reminder: '{title_txt}' are deadline maine ({due_txt})."
    if reminder_type == 'due_today':
        return f"Reminder: '{title_txt}' are deadline azi ({due_txt})."
    return f"Overdue: '{title_txt}' are deadline depasit ({due_txt})."


def _generate_task_reminders_for_user(user):
    today = timezone.localdate()
    tasks_qs = (
        Task.objects
        .filter(due_date__isnull=False)
        .exclude(status__in=['done', 'archived'])
        .filter(
            models.Q(assignee=user)
            | (models.Q(assignee__isnull=True) & models.Q(created_by=user))
        )
        .only('id', 'title', 'due_date')
    )
    task_list = list(tasks_qs)
    if not task_list:
        return 0

    existing = set(
        TaskReminderLog.objects.filter(
            user=user,
            reminder_date=today,
            task_id__in=[t.id for t in task_list],
        ).values_list('task_id', 'reminder_type')
    )

    notif_payload = []
    log_payload = []
    for task in task_list:
        reminder_type = _task_reminder_type_for_date(task.due_date, today)
        if not reminder_type:
            continue
        key = (task.id, reminder_type)
        if key in existing:
            continue
        notif_payload.append(
            Notification(
                user=user,
                message=_task_reminder_text(task, reminder_type)[:255],
                url=reverse('tasks'),
                notif_type='task',
            )
        )
        log_payload.append(
            TaskReminderLog(
                user=user,
                task=task,
                reminder_type=reminder_type,
                reminder_date=today,
            )
        )
        existing.add(key)

    if notif_payload:
        Notification.objects.bulk_create(notif_payload)
        TaskReminderLog.objects.bulk_create(log_payload)
    return len(notif_payload)


def home(request):
    if not request.user.is_authenticated:
        return redirect('login')

    from .models import Task, ChatMessage

    # tasks relevant to user
    if request.user.is_staff or request.user.is_superuser:
        task_qs = Task.objects.all()
    else:
        task_qs = Task.objects.filter(
            models.Q(assignee=request.user) | models.Q(created_by=request.user)
        )

    todo_count = task_qs.filter(status='todo').count()
    revision_count = task_qs.filter(status='revision').count()
    done_count = task_qs.filter(status='done').count()
    total_tasks = task_qs.count()

    # unread messages simple count: (all messages not sent by user) since last visit
    last_seen_msg_key = 'chat_seen_global'
    last_seen_msg = request.session.get(last_seen_msg_key)
    last_seen_dt = None
    if last_seen_msg:
        try:
            last_seen_dt = timezone.datetime.fromisoformat(last_seen_msg)
            if timezone.is_naive(last_seen_dt):
                last_seen_dt = timezone.make_aware(last_seen_dt, timezone.get_current_timezone())
        except Exception:
            last_seen_dt = None

    msg_qs = ChatMessage.objects.exclude(sender=request.user)
    if last_seen_dt:
        msg_qs = msg_qs.filter(created_at__gt=last_seen_dt)
    unread_messages = msg_qs.count()

    # update last seen for next time
    request.session[last_seen_msg_key] = timezone.now().isoformat()
    request.session.modified = True

    # upcoming meetings from Meeting model
    upcoming_meetings = Meeting.objects.filter(date__gte=timezone.localdate()).order_by('date', 'time')[:5]

    # weekly activity = procent task-uri done în ultimele 7 zile / total
    week_ago = timezone.now() - timezone.timedelta(days=7)
    done_week = task_qs.filter(status='done', updated_at__gte=week_ago).count() if task_qs.model._meta.get_field('updated_at') else 0
    weekly_activity = 0
    if total_tasks:
        weekly_activity = round(done_week / total_tasks * 100)

    # leave requests / vacation placeholder
    vacation_people = []

    context = {
        'total_tasks': total_tasks,
        'todo_count': todo_count,
        'revision_count': revision_count,
        'done_count': done_count,
        'unread_messages': unread_messages,
        'upcoming_meetings': upcoming_meetings,
        'weekly_activity': weekly_activity,
        'vacation_people': vacation_people,
        'can_manage_meetings': _is_admin_user(request.user),
        'calendar_sync_url': _calendar_sync_url(request, request.user),
        'google_calendar_connected': _google_calendar_connected(request.user),
        'google_calendar_ready': _google_oauth_ready(),
    }
    return render(request, 'core/dashboard.html', context)


# alias pentru dashboard
dashboard = login_required(home)


@login_required
def add_meeting(request):
    today = timezone.localdate()
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        date = request.POST.get('date')
        time = request.POST.get('time') or None
        location = request.POST.get('location', '').strip()
        participants = request.POST.get('participants', '').strip()
        description = request.POST.get('description', '').strip()
        if title and date:
            meeting = Meeting.objects.create(
                title=title,
                date=date,
                time=time,
                location=location,
                participants=participants,
                description=description,
                created_by=request.user,
            )
            # Ensure typed values for date/time before notifications + Google sync.
            try:
                meeting.refresh_from_db(fields=['date', 'time'])
            except Exception:
                pass
            try:
                date_txt = meeting.date.strftime('%d.%m.%Y')
                if meeting.time:
                    message = f"Meeting nou: {meeting.title} pe {date_txt} la {meeting.time.strftime('%H:%M')}"
                else:
                    message = f"Meeting nou: {meeting.title} pe {date_txt}"
                users = User.objects.filter(is_active=True).only('id')
                Notification.objects.bulk_create([
                    Notification(
                        user=u,
                        message=message[:255],
                        url=reverse('dashboard'),
                        notif_type='other',
                    )
                    for u in users
                ])
            except (DatabaseError, AttributeError, TypeError, ValueError):
                # Nu blocăm salvarea meeting-ului dacă notificările nu pot fi scrise.
                pass
            if _google_calendar_connected(request.user):
                ok, err = _sync_meeting_to_google(meeting)
                if not ok:
                    err_txt = (err or 'unknown_error')
                    messages.warning(request, f'Meeting salvat, dar sincronizarea Google Calendar a eșuat: {err_txt}')
            else:
                messages.info(request, 'Meeting salvat local. Conectează Google Calendar pentru sincronizare.')
            return redirect('home')

    # calendar data for current month
    import calendar
    month = today.month
    year = today.year
    cal = calendar.monthcalendar(year, month)
    meetings = Meeting.objects.filter(date__year=year, date__month=month)
    meetings_by_day = {}
    for m in meetings:
        meetings_by_day.setdefault(m.date.day, []).append(m)

    context = {
        'month_name': calendar.month_name[month],
        'year': year,
        'cal_rows': cal,
        'meetings_by_day': meetings_by_day,
        'today_day': today.day,
        'days_with_meetings': list(meetings_by_day.keys()),
    }
    return render(request, 'core/add-meeting.html', context)


@login_required
def delete_meeting(request, meeting_id):
    if request.method != 'POST':
        return redirect('dashboard')

    meeting = get_object_or_404(Meeting, pk=meeting_id)
    can_delete = (meeting.created_by_id == request.user.id) or _is_admin_user(request.user)
    if not can_delete:
        messages.error(request, "Nu ai permisiunea să ștergi acest meeting.")
        return redirect('dashboard')

    meeting_title = meeting.title
    meeting_owner = meeting.created_by
    google_event_id = meeting.google_event_id
    meeting.delete()

    try:
        users = User.objects.filter(is_active=True).only('id')
        Notification.objects.bulk_create([
            Notification(
                user=u,
                message=f"Meeting anulat: {meeting_title}"[:255],
                url=reverse('dashboard'),
                notif_type='other',
            )
            for u in users
        ])
    except DatabaseError:
        pass

    if google_event_id:
        ok, _ = _delete_meeting_from_google(meeting_owner, google_event_id)
        if not ok:
            messages.warning(request, 'Meeting șters local, dar ștergerea din Google Calendar a eșuat.')

    return redirect('dashboard')


def calendar_feed_ics(request, token):
    profile = Profile.objects.select_related('user').filter(calendar_feed_token=token).first()
    if not profile or not profile.user.is_active:
        raise Http404("Calendar feed not found.")

    feed_user = profile.user
    today = timezone.localdate()
    from_date = today - timedelta(days=30)
    to_date = today + timedelta(days=365)

    if _is_admin_user(feed_user):
        meetings_qs = Meeting.objects.all()
    else:
        participant_q = models.Q(created_by=feed_user) | models.Q(participants__exact='')
        if feed_user.username:
            participant_q = participant_q | models.Q(participants__icontains=feed_user.username)
        if feed_user.email:
            participant_q = participant_q | models.Q(participants__icontains=feed_user.email)
        full_name = (feed_user.get_full_name() or '').strip()
        if full_name:
            participant_q = participant_q | models.Q(participants__icontains=full_name)
        meetings_qs = Meeting.objects.filter(participant_q)

    meetings = meetings_qs.filter(date__gte=from_date, date__lte=to_date).order_by('date', 'time', 'id')

    now_utc = _ics_dt_utc(timezone.now())
    cal_name = _ics_escape(f"Panel meetings - {feed_user.get_full_name() or feed_user.username}")
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Panel//Meetings Calendar//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        f'X-WR-CALNAME:{cal_name}',
        'X-WR-TIMEZONE:Europe/Bucharest',
    ]

    for m in meetings:
        uid = f'panel-meeting-{m.id}@signup.local'
        lines.append('BEGIN:VEVENT')
        lines.append(f'UID:{uid}')
        lines.append(f'DTSTAMP:{now_utc}')

        if m.time:
            start_dt = datetime.combine(m.date, m.time)
            start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
            end_dt = start_dt + timedelta(hours=1)
            lines.append(f'DTSTART:{_ics_dt_utc(start_dt)}')
            lines.append(f'DTEND:{_ics_dt_utc(end_dt)}')
        else:
            start_d = m.date.strftime('%Y%m%d')
            end_d = (m.date + timedelta(days=1)).strftime('%Y%m%d')
            lines.append(f'DTSTART;VALUE=DATE:{start_d}')
            lines.append(f'DTEND;VALUE=DATE:{end_d}')

        lines.append(f'SUMMARY:{_ics_escape(m.title)}')
        if m.location:
            lines.append(f'LOCATION:{_ics_escape(m.location)}')

        desc_parts = []
        if m.description:
            desc_parts.append(m.description)
        if m.participants:
            desc_parts.append(f'Participants: {m.participants}')
        desc_parts.append(f'Created by: {m.created_by.get_full_name() or m.created_by.username}')
        desc_text = '\n'.join(desc_parts)
        lines.append(f'DESCRIPTION:{_ics_escape(desc_text)}')
        lines.append('END:VEVENT')

    lines.append('END:VCALENDAR')
    ics_data = '\r\n'.join(lines) + '\r\n'

    response = HttpResponse(ics_data, content_type='text/calendar; charset=utf-8')
    safe_username = (feed_user.username or 'user').replace(' ', '_')
    response['Content-Disposition'] = f'inline; filename="panel_meetings_{safe_username}.ics"'
    response['Cache-Control'] = 'no-cache'
    return response


@login_required
def regenerate_calendar_feed_token(request):
    prof = _ensure_profile(request.user)
    if request.method == 'POST':
        prof.calendar_feed_token = uuid.uuid4()
        prof.save(update_fields=['calendar_feed_token'])
        messages.success(request, 'Calendar sync link regenerated.')
    next_url = request.POST.get('next') or request.GET.get('next') or reverse('dashboard')
    return redirect(next_url)


@login_required
def google_calendar_connect(request):
    next_url = request.GET.get('next') or request.POST.get('next') or reverse('dashboard')
    if not _google_oauth_ready():
        messages.error(request, 'Google Calendar nu este configurat pe server.')
        return redirect(next_url)

    client_id, _ = _google_oauth_credentials()
    redirect_uri = _google_redirect_uri(request)
    state = uuid.uuid4().hex
    request.session['google_oauth_state'] = state
    request.session['google_oauth_next'] = next_url
    request.session.modified = True

    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': GOOGLE_OAUTH_SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
    }
    auth_url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return redirect(auth_url)


@login_required
def google_calendar_callback(request):
    next_url = request.session.pop('google_oauth_next', reverse('dashboard'))
    expected_state = request.session.pop('google_oauth_state', '')
    state = (request.GET.get('state') or '').strip()
    if not expected_state or state != expected_state:
        messages.error(request, 'Google connect failed: invalid state.')
        return redirect(next_url)

    error = (request.GET.get('error') or '').strip()
    if error:
        messages.error(request, f'Google connect failed: {error}')
        return redirect(next_url)

    code = (request.GET.get('code') or '').strip()
    if not code:
        messages.error(request, 'Google connect failed: missing authorization code.')
        return redirect(next_url)

    client_id, client_secret = _google_oauth_credentials()
    redirect_uri = _google_redirect_uri(request)
    if not (client_id and client_secret):
        messages.error(request, 'Google Calendar nu este configurat pe server.')
        return redirect(next_url)

    try:
        data = _google_token_exchange({
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        })
    except Exception as exc:
        messages.error(request, f'Google connect failed: {exc}')
        return redirect(next_url)

    access_token = (data.get('access_token') or '').strip()
    refresh_token = (data.get('refresh_token') or '').strip()
    if not access_token:
        messages.error(request, 'Google connect failed: no access token returned.')
        return redirect(next_url)

    conn, _ = GoogleCalendarConnection.objects.get_or_create(user=request.user)
    if refresh_token:
        conn.refresh_token = refresh_token
    elif not conn.refresh_token:
        messages.error(request, 'Google nu a trimis refresh token. Șterge access-ul aplicației și reconectează.')
        return redirect(next_url)

    expires_in = int(data.get('expires_in') or 3600)
    conn.access_token = access_token
    conn.access_token_expires_at = timezone.now() + timedelta(seconds=max(expires_in - 30, 60))
    if not conn.calendar_id:
        conn.calendar_id = 'primary'
    conn.sync_enabled = True
    conn.save()

    synced = 0
    failed = 0
    future_meetings = Meeting.objects.filter(
        created_by=request.user,
        date__gte=timezone.localdate() - timedelta(days=30),
    ).order_by('date', 'time', 'id')[:200]
    for mt in future_meetings:
        ok, _ = _sync_meeting_to_google(mt)
        if ok:
            synced += 1
        else:
            failed += 1

    messages.success(request, f'Google Calendar connected. Synced {synced} meeting(s).')
    if failed:
        messages.warning(request, f'{failed} meeting(s) could not be synced.')
    return redirect(next_url)


@login_required
def google_calendar_disconnect(request):
    next_url = request.POST.get('next') or request.GET.get('next') or reverse('dashboard')
    if request.method == 'POST':
        GoogleCalendarConnection.objects.filter(user=request.user).delete()
        Meeting.objects.filter(created_by=request.user).update(google_event_id='')
        messages.success(request, 'Google Calendar disconnected.')
    return redirect(next_url)


@login_required
def add_submission(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    if not (_is_admin_user(request.user) or task.assignee_id == request.user.id or task.created_by_id == request.user.id):
        return redirect('tasks')
    error = None
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        file = request.FILES.get('file')
        if not description and not file:
            error = "Adaugă o descriere sau un fișier."
        else:
            Submission.objects.create(
                task=task,
                author=request.user,
                description=description,
                file=file
            )
            if task.status != 'revision':
                task.status = 'revision'
                task.save(update_fields=['status', 'updated_at'])
            return redirect('see_submission', task_id=task.id)

    return render(request, 'core/add-submission.html', {'task': task, 'error': error})


@login_required
def see_submission(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    if not (_is_admin_user(request.user) or task.assignee_id == request.user.id or task.created_by_id == request.user.id):
        return redirect('tasks')

    submissions_qs = Submission.objects.select_related('author').filter(task=task).order_by('-created_at')
    if _is_admin_user(request.user) or task.created_by_id == request.user.id:
        submission = submissions_qs.first()
    else:
        submission = submissions_qs.filter(author=request.user).first()
    return render(request, 'core/see-submission.html', {'task': task, 'submission': submission})


@login_required
def submissions_admin(request):
    # doar admini
    if not (request.user.is_staff or request.user.is_superuser or 'admin' in request.user.username):
        return redirect('tasks')

    qs = Submission.objects.select_related('task', 'author').all()

    task_id = request.GET.get('task')
    if task_id:
        qs = qs.filter(task_id=task_id)

    if request.method == 'POST':
        sub_id = request.POST.get('sub_id')
        action = request.POST.get('action')
        comment = request.POST.get('comment', '').strip()
        sub = Submission.objects.filter(id=sub_id).first()
        if sub and action in dict(Submission.STATUS_CHOICES):
            sub.status = action
            if comment:
                sub.reviewer_comment = comment
            sub.save(update_fields=['status', 'reviewer_comment', 'updated_at'])
            messages.success(request, 'Submission updated')
        return redirect('submissions_admin')

    return render(request, 'core/submissions-admin.html', {
        'submissions': qs,
    })


@login_required
def profile_public(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    prof = getattr(target_user, 'profile', None)
    if prof is None:
        prof, _ = Profile.objects.get_or_create(user=target_user)

    can_edit_function = (_is_admin_user(request.user) or request.user.id == target_user.id)
    if request.method == 'POST':
        if not can_edit_function:
            messages.error(request, "Nu ai permisiunea să modifici funcția acestui user.")
            return redirect('profile_public', user_id=target_user.id)

        new_function = (request.POST.get('function') or '').strip()
        if not new_function:
            messages.error(request, "Funcția nu poate fi goală.")
            return redirect('profile_public', user_id=target_user.id)

        prof.function = new_function[:100]
        prof.save(update_fields=['function'])
        messages.success(request, "Funcția a fost actualizată.")
        return redirect('profile_public', user_id=target_user.id)

    skills_list = [s.strip() for s in (prof.skills or '').split(',') if prof and prof.skills] if prof else []
    languages_list = [s.strip() for s in (prof.languages or '').split(',') if prof and prof.languages] if prof else []
    return render(request, 'core/profile-public.html', {
        'target_user': target_user,
        'profile_obj': prof,
        'skills_list': skills_list,
        'languages_list': languages_list,
        'can_edit_function': can_edit_function,
    })


def logout_user(request):
    from django.contrib.auth import logout
    logout(request)
    return redirect('login')


@login_required
def search_user(request):
    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'results': []})
    users = User.objects.filter(
        models.Q(username__icontains=q)
        | models.Q(email__icontains=q)
        | models.Q(first_name__icontains=q)
        | models.Q(last_name__icontains=q)
    )[:10]
    res = [
        {
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'full_name': (u.get_full_name() or u.username),
        }
        for u in users
    ]
    return JsonResponse({'results': res})


@login_required
def notifications_api(request):
    try:
        try:
            _generate_task_reminders_for_user(request.user)
        except Exception:
            pass

        # Deliver due bot reminders (no background worker needed)
        try:
            now = timezone.now()
            due = BotReminder.objects.filter(user=request.user, delivered_at__isnull=True, remind_at__lte=now)[:5]
            if due:
                Notification.objects.bulk_create([
                    Notification(
                        user=request.user,
                        message=f"Reminder: {r.message}"[:255],
                        url='',
                        notif_type='other',
                    )
                    for r in due
                ])
                BotReminder.objects.filter(id__in=[r.id for r in due]).update(delivered_at=now)
        except Exception:
            pass

        def _local_hhmm(dt):
            if not dt:
                return ''
            try:
                return timezone.localtime(dt).strftime('%H:%M')
            except Exception:
                return dt.strftime('%H:%M')

        qs = Notification.objects.filter(user=request.user, is_read=False).order_by('-created_at')[:20]
        results = []
        for n in qs:
            results.append({
                'id': n.id,
                'text': n.message,
                'url': n.url or '',
                'time': _local_hhmm(n.created_at),
                'is_read': n.is_read,
                'type': n.notif_type,
            })

        # fallback dacă nu există deloc notificări (nu blocăm UX)
        if not results:
            since = timezone.now() - timezone.timedelta(days=2)
            # task-uri noi pentru user
            t_qs = Task.objects.filter(assignee=request.user, created_at__gte=since).order_by('-created_at')[:5]
            for t in t_qs:
                results.append({
                    'id': f'task-{t.id}',
                    'text': f'Task nou: {t.title}',
                    'url': '/tasks/',
                    'time': _local_hhmm(t.created_at),
                    'is_read': False,
                    'type': 'task',
                })
            # holiday request status pentru user
            h_qs = HolidayRequest.objects.filter(user=request.user, updated_at__gte=since).order_by('-updated_at')[:5] if hasattr(HolidayRequest, 'updated_at') else []
            for h in h_qs:
                results.append({
                    'id': f'holiday-{h.id}',
                    'text': f'Cereră concediu {h.get_status_display()}',
                    'url': '/calendar/requests/',
                    'time': _local_hhmm(h.updated_at) if hasattr(h, 'updated_at') else '',
                    'is_read': False,
                    'type': 'holiday',
                })

        return JsonResponse({'results': results})
    except Exception:
        # dacă tabela nu există sau altă eroare, nu blocăm UI
        return JsonResponse({'results': []})


@login_required
def notifications_mark(request):
    if request.method == 'POST':
        notif_id = request.GET.get('read')
        if notif_id == 'all':
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        elif notif_id:
            try:
                Notification.objects.filter(user=request.user, id=notif_id).update(is_read=True)
            except Exception:
                pass
        else:
            # mark all for this user
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'ok': True})


@login_required
def bot_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    message = ''
    try:
        if request.body:
            data = json.loads(request.body.decode('utf-8') or '{}')
            message = (data.get('message') or '').strip()
    except Exception:
        message = ''
    if not message:
        message = (request.POST.get('message') or '').strip()
    # Use OpenAI if configured, otherwise fallback to local bot.
    reply = _bot_answer(message, request=request)

    # persist conversation
    try:
        if request.user.is_authenticated and message:
            BotMessage.objects.create(user=request.user, role='user', content=message)
            BotMessage.objects.create(user=request.user, role='assistant', content=reply)
    except Exception:
        pass

    return JsonResponse({
        'reply': reply,
        'suggestions': _bot_suggestions_for_topic(request.session.get('bot_last_topic'))
    })


@login_required
def bot_history_api(request):
    qs = BotMessage.objects.filter(user=request.user).order_by('-created_at')[:40]
    items = []
    for m in reversed(list(qs)):
        items.append({
            'role': m.role,
            'content': m.content,
            'time': timezone.localtime(m.created_at).strftime('%H:%M'),
        })
    return JsonResponse({'results': items})


def _unique_username(base_username):
    username = base_username
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base_username}{counter}"
        counter += 1
    return username


def signup(request):
    if request.method == 'POST':
        first = request.POST.get('first_name', '').strip()
        last = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password1 = request.POST.get('password1') or ''
        password2 = request.POST.get('password2') or ''
        role = request.POST.get('role', 'worker')

        if not all([first, last, email, password1, password2]):
            messages.error(request, "Completează toate câmpurile.")
            return render(request, 'core/index.html')

        if password1 != password2:
            messages.error(request, "Parolele nu coincid.")
            return render(request, 'core/index.html')

        if User.objects.filter(email=email).exists():
            messages.error(request, "Există deja un cont cu acest email.")
            return render(request, 'core/index.html')

        base_username = email if email else f"{first}{last}".lower()
        username = _unique_username(base_username)

        try:
            validate_password(password1)
        except ValidationError as e:
            for err in e:
                messages.error(request, err)
            return render(request, 'core/index.html')

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password1,
            first_name=first,
            last_name=last,
        )

        # completează profilul
        profile = getattr(user, 'profile', None)
        if profile:
            profile.full_name = f"{first} {last}".strip()
            profile.function = "Admin" if role == 'admin' else "Worker"
            profile.save()

        # login automat
        auth_user = authenticate(username=username, password=password1)
        if auth_user:
            login(request, auth_user)
            messages.success(request, "Cont creat și autentificat cu succes.")
            return redirect('team')

        messages.success(request, "Cont creat. Autentifică-te.")
        return redirect('login')

    return render(request, 'core/index.html')


@login_required
def team(request):
    members = Profile.objects.select_related('user').all()
    members_data = []
    for p in members:
        members_data.append({
            'id': p.user.id,
            'name': p.full_name or p.user.get_username(),
            'role': p.function or 'Member',
            'username': p.user.username,
            'email': p.user.email or '',
            'avatar': p.avatar.url if p.avatar else '',
            'specialty': p.function or 'Other',
        })
    return render(request, 'core/team.html', {
        'members': members,
        'members_json': json.dumps(members_data),
    })


@login_required
def profile(request):
    prof = getattr(request.user, 'profile', None)
    if prof is None:
        prof, _ = Profile.objects.get_or_create(user=request.user)
    fn = (prof.function or '').lower() if prof else ''
    admin_labels = {'admin', 'administrator', 'manager', 'owner'}
    is_admin = request.user.is_superuser or request.user.is_staff or fn in admin_labels
    template = 'core/profile-admin.html' if is_admin else 'core/profile-worker.html'

    if request.method == 'POST' and prof:
        prof.full_name = (request.POST.get('full_name') or prof.full_name or '').strip()
        prof.function = (request.POST.get('function') or prof.function or '').strip()
        prof.about = (request.POST.get('about') or prof.about or '').strip()
        prof.languages = (request.POST.get('languages') or prof.languages or '').strip()
        prof.skills = (request.POST.get('skills') or prof.skills or '').strip()
        prof.experience = (request.POST.get('experience') or prof.experience or '').strip()
        prof.education = (request.POST.get('education') or prof.education or '').strip()
        if 'avatar' in request.FILES:
            prof.avatar = request.FILES['avatar']
        # basic email update
        email = (request.POST.get('email') or '').strip()
        if email and email != request.user.email:
            request.user.email = email
            request.user.save(update_fields=['email'])
        prof.save()
        return redirect('profile')

    skills_list = [s.strip() for s in (prof.skills or '').split(',') if s.strip()]
    languages_list = [s.strip() for s in (prof.languages or '').split(',') if s.strip()]
    context = {
        'profile': prof,
        'skills_list': skills_list,
        'languages_list': languages_list,
    }
    if not is_admin:
        context['leave_balance'] = _leave_balance_for_user(request.user)
    return render(request, template, context)


@login_required
def export_tasks_csv(request):
    if not _is_admin_user(request.user):
        return redirect('tasks')

    def _task_row(t):
        assignee_name = t.assignee.get_full_name() if t.assignee else ''
        assignee_name = assignee_name or (t.assignee.username if t.assignee else '')
        assignee_email = t.assignee.email if t.assignee else ''
        created_name = t.created_by.get_full_name() or t.created_by.username
        return [
            t.id,
            t.title,
            t.get_status_display(),
            t.get_priority_display(),
            assignee_name,
            assignee_email,
            created_name,
            t.due_date.isoformat() if t.due_date else '',
            (t.description or '').replace('\r\n', '\n').replace('\r', '\n'),
            t.comments.count(),
            timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'),
            timezone.localtime(t.updated_at).strftime('%Y-%m-%d %H:%M'),
        ]

    status = (request.GET.get('status') or '').strip().lower()
    valid_statuses = {k for k, _ in Task.STATUS_CHOICES}
    qs = Task.objects.select_related('assignee', 'created_by').order_by('-created_at')
    if status in valid_statuses:
        qs = qs.filter(status=status)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    filename = f"tasks_{timezone.localdate().isoformat()}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow([
        'ID',
        'Title',
        'Status',
        'Priority',
        'Assignee',
        'Assignee Email',
        'Created By',
        'Due Date',
        'Description',
        'Comments Count',
        'Created At',
        'Updated At',
    ])
    for t in qs:
        writer.writerow(_task_row(t))
    return response


@login_required
def export_task_csv(request, task_id):
    task = get_object_or_404(
        Task.objects.select_related('assignee', 'created_by').prefetch_related(
            models.Prefetch('comments', queryset=TaskComment.objects.select_related('author').order_by('created_at')),
            models.Prefetch('submissions', queryset=Submission.objects.select_related('author').order_by('created_at')),
        ),
        id=task_id,
    )

    can_access = (
        _is_admin_user(request.user)
        or task.created_by_id == request.user.id
        or task.assignee_id == request.user.id
    )
    if not can_access:
        return redirect('tasks')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    filename = f"task_{task.id}_{timezone.localdate().isoformat()}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    writer = csv.writer(response)

    assignee_name = task.assignee.get_full_name() if task.assignee else ''
    assignee_name = assignee_name or (task.assignee.username if task.assignee else '')
    assignee_email = task.assignee.email if task.assignee else ''
    created_name = task.created_by.get_full_name() or task.created_by.username

    writer.writerow([
        'Task ID',
        'Title',
        'Status',
        'Priority',
        'Assignee',
        'Assignee Email',
        'Created By',
        'Due Date',
        'Description',
        'Comments Count',
        'Created At',
        'Updated At',
    ])
    writer.writerow([
        task.id,
        task.title,
        task.get_status_display(),
        task.get_priority_display(),
        assignee_name,
        assignee_email,
        created_name,
        task.due_date.isoformat() if task.due_date else '',
        (task.description or '').replace('\r\n', '\n').replace('\r', '\n'),
        task.comments.count(),
        timezone.localtime(task.created_at).strftime('%Y-%m-%d %H:%M'),
        timezone.localtime(task.updated_at).strftime('%Y-%m-%d %H:%M'),
    ])

    writer.writerow([])
    writer.writerow(['Comments'])
    writer.writerow(['Author', 'Comment', 'Created At'])
    comments_qs = task.comments.all()
    if comments_qs:
        for c in comments_qs:
            author = c.author.get_full_name() or c.author.username
            writer.writerow([
                author,
                (c.body or '').replace('\r\n', '\n').replace('\r', '\n'),
                timezone.localtime(c.created_at).strftime('%Y-%m-%d %H:%M'),
            ])
    else:
        writer.writerow(['', 'No comments', ''])

    writer.writerow([])
    writer.writerow(['Submissions'])
    writer.writerow(['Author', 'Status', 'Description', 'File', 'Created At'])
    submissions_qs = task.submissions.all()
    if submissions_qs:
        for s in submissions_qs:
            author = s.author.get_full_name() or s.author.username
            writer.writerow([
                author,
                s.get_status_display(),
                (s.description or '').replace('\r\n', '\n').replace('\r', '\n'),
                s.file.name if s.file else '',
                timezone.localtime(s.created_at).strftime('%Y-%m-%d %H:%M'),
            ])
    else:
        writer.writerow(['', 'No submissions', '', '', ''])

    return response


@login_required
def tasks(request):
    # Determine admin vs worker
    is_admin = request.user.is_staff or request.user.is_superuser or 'admin' in request.user.username

    comments_qs = TaskComment.objects.select_related('author').order_by('-created_at')
    # Determine queryset: managers see all, workers see own
    if is_admin:
        qs = Task.objects.select_related('assignee', 'created_by').prefetch_related(
            models.Prefetch('comments', queryset=comments_qs)
        )
    else:
        qs = Task.objects.select_related('assignee', 'created_by').prefetch_related(
            models.Prefetch('comments', queryset=comments_qs)
        ).filter(models.Q(assignee=request.user) | models.Q(created_by=request.user))

    if request.method == 'POST':
        comment_task_id = request.POST.get('comment_task_id')
        comment_text = (request.POST.get('comment_text') or '').strip()
        if comment_task_id:
            task_obj = qs.filter(id=comment_task_id).first()
            if task_obj and comment_text:
                TaskComment.objects.create(
                    task=task_obj,
                    author=request.user,
                    body=comment_text[:600],
                )
                notify_ids = set()
                if task_obj.created_by_id and task_obj.created_by_id != request.user.id:
                    notify_ids.add(task_obj.created_by_id)
                if task_obj.assignee_id and task_obj.assignee_id != request.user.id:
                    notify_ids.add(task_obj.assignee_id)
                if notify_ids:
                    title_txt = task_obj.title[:40] + ('…' if len(task_obj.title) > 40 else '')
                    msg_txt = comment_text[:70] + ('…' if len(comment_text) > 70 else '')
                    Notification.objects.bulk_create([
                        Notification(
                            user=u,
                            message=f"Comentariu nou la {title_txt}: {msg_txt}"[:255],
                            url=reverse('tasks'),
                            notif_type='task',
                        )
                        for u in User.objects.filter(id__in=notify_ids, is_active=True).only('id')
                    ])
            return redirect('tasks')

        if not is_admin:
            return redirect('tasks')

    # Create/update task (admin)
    if request.method == 'POST':
        # mark done
        update_task_id = request.POST.get('update_task_id')
        if update_task_id:
            task = Task.objects.filter(id=update_task_id).first()
            if task:
                task.status = 'done'
                task.save(update_fields=['status', 'updated_at'])
            return redirect('tasks')

        # archive task (visible doar după Done)
        archive_task_id = request.POST.get('archive_task_id')
        if archive_task_id:
            task = Task.objects.filter(id=archive_task_id).first()
            if task:
                task.status = 'archived'
                task.save(update_fields=['status', 'updated_at'])
            return redirect('tasks')

        # unarchive task (revine în done)
        unarchive_task_id = request.POST.get('unarchive_task_id')
        if unarchive_task_id:
            task = Task.objects.filter(id=unarchive_task_id).first()
            if task:
                task.status = 'done'
                task.save(update_fields=['status', 'updated_at'])
            return redirect('tasks')

        # delete task (admin explicit)
        delete_task_id = request.POST.get('delete_task_id')
        if delete_task_id:
            Task.objects.filter(id=delete_task_id).delete()
            return redirect('tasks')

        # create task
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        technologies = request.POST.get('technologies', '').strip()
        status = request.POST.get('status', 'todo')
        priority = request.POST.get('priority', 'medium')
        due_date = request.POST.get('due_date') or None
        assignee_ids = request.POST.getlist('assignees') or []
        from django.contrib.auth import get_user_model
        user_model = get_user_model()

        full_description = description
        if technologies:
            tech_line = f"Technologies: {technologies}"
            full_description = f"{description}\n{tech_line}" if description else tech_line

        # If multiple assignees selected, create a task per assignee
        targets = user_model.objects.filter(id__in=assignee_ids) if assignee_ids else [None]
        for person in targets:
            Task.objects.create(
                title=title or "Untitled task",
                description=full_description,
                status=status if status in dict(Task.STATUS_CHOICES) else 'todo',
                priority=priority if priority in dict(Task.PRIORITY_CHOICES) else 'medium',
                due_date=due_date or None,
                assignee=person,
                created_by=request.user,
            )
        return redirect('tasks')

    tasks_by_status = {
        'todo': qs.filter(status='todo'),
        'revision': qs.filter(status='revision'),
        'done': qs.filter(status='done'),
        'archived': qs.filter(status='archived'),
    }

    board_cols = [
        ('todo', 'TO DO', tasks_by_status['todo']),
        ('revision', 'IN REVISION', tasks_by_status['revision']),
        ('done', 'DONE', tasks_by_status['done']),
        ('archived', 'ARCHIVED', tasks_by_status['archived']),
    ]

    from django.contrib.auth import get_user_model
    users = get_user_model().objects.all()
    if is_admin:
        return render(request, 'core/tasks-admin.html', {
            'tasks_list': qs,
            'users': users,
            'task': Task,  # for choices
            'board_cols': board_cols,
        })

    return render(request, 'core/task-main-worker.html', {
        'tasks_by_status': tasks_by_status,
        'board_cols': board_cols,
        'users': users,
    })


def _chat_thread_url(thread_id):
    return f"{reverse('chat')}?thread={thread_id}"


def _chat_user_can_access_thread(user, thread):
    if thread.thread_type == 'group':
        return True
    return thread.participants.filter(id=user.id).exists()


def _chat_active_mute_q(now_ts=None):
    now_ts = now_ts or timezone.now()
    return models.Q(muted_until__isnull=True) | models.Q(muted_until__gt=now_ts)


def _chat_get_active_mute(user, thread, now_ts=None):
    now_ts = now_ts or timezone.now()
    return ChatThreadMute.objects.filter(user=user, thread=thread).filter(_chat_active_mute_q(now_ts)).first()


def _chat_recipients_ids(thread, exclude_user_id):
    if thread.thread_type == 'group':
        return list(User.objects.filter(is_active=True).exclude(id=exclude_user_id).values_list('id', flat=True))
    return list(thread.participants.exclude(id=exclude_user_id).values_list('id', flat=True))


def _chat_mark_thread_read(user, thread, at_ts=None):
    at_ts = at_ts or timezone.now()
    ChatThreadReadState.objects.update_or_create(
        user=user,
        thread=thread,
        defaults={'last_read_at': at_ts},
    )


def _chat_dm_online(dm_user):
    if not dm_user:
        return False
    try:
        ls = dm_user.profile.last_seen
        if ls:
            if timezone.is_naive(ls):
                ls = timezone.make_aware(ls, timezone.get_current_timezone())
            return (timezone.now() - ls) <= timedelta(minutes=5)
    except Exception:
        return False
    return False


def _chat_typing_users(thread, exclude_user_id=None, now_ts=None):
    now_ts = now_ts or timezone.now()
    cutoff = now_ts - timedelta(seconds=8)
    qs = ChatTypingState.objects.select_related('user').filter(thread=thread, last_typed_at__gte=cutoff)
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return [{'user_id': row.user_id, 'username': row.user.username} for row in qs]


def _chat_mute_value_and_note(mute_obj, now_ts=None):
    now_ts = now_ts or timezone.now()
    if not mute_obj:
        return '0', ''
    if mute_obj.muted_until is None:
        return 'forever', 'Muted until manual unmute.'
    if mute_obj.muted_until <= now_ts:
        return '0', ''
    remaining = (mute_obj.muted_until - now_ts).total_seconds()
    if remaining <= 5400:
        value = '1h'
    elif remaining <= 32400:
        value = '8h'
    else:
        value = '24h'
    note = f"Muted until {timezone.localtime(mute_obj.muted_until).strftime('%d.%m %H:%M')}"
    return value, note


@login_required
def chat(request):
    emoji_list = [
        "😀","😃","😄","😁","😆","😅","😂","🤣","😊","😇","🙂","🙃","😉","😌",
        "😍","🥰","😘","😗","😙","😚","😋","😛","😜","🤪","🤨","🧐","🤓","😎",
        "🤩","🥳","😏","😒","😞","😔","😟","😕","🙁","☹️","😣","😖","😫","😩",
        "🥺","😢","😭","😤","😠","😡","🤬","🤯","😳","🥵","🥶","😱","😨","😰",
        "😥","😓","🤗","🤔","🤭","🤫","🤥","😶","😐","😑","😬","🙄","😯","😦",
        "😧","😮","😲","🥱","😴","🤤","😪","😵","🤐","🤑","🤠","😷","🤒","🤕",
        "🤢","🤮","🤧","😈","👿","💀","☠️","👻","👽","🤖",
        "💩","👏","👍","👎","👊","✊","🤛","🤜","🤝","🙏","🤲","🙌","🙋","🤦",
        "🤷","💪","🧠","🫶","❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔",
        "❣️","💕","💞","💓","💗","💖","💘","💝","💟","☀️","⭐","🌟","⚡","🔥",
        "✨","🎉","🎊","🎁","🚀","✈️","🛠️","⌛","🕒","🕓","🕔","🕙","🕛"
    ]

    now_ts = timezone.now()
    try:
        ChatThreadMute.objects.filter(muted_until__isnull=False, muted_until__lte=now_ts).delete()
        ChatTypingState.objects.filter(last_typed_at__lt=now_ts - timedelta(minutes=5)).delete()
    except DatabaseError:
        pass

    group_thread, _ = ChatThread.objects.get_or_create(
        thread_type='group',
        name='Team Chat',
        defaults={'created_by': request.user},
    )

    users = User.objects.exclude(id=request.user.id)
    dm_threads = []
    for u in users:
        name = f"dm-{min(u.id, request.user.id)}-{max(u.id, request.user.id)}"
        thread, _ = ChatThread.objects.get_or_create(thread_type='dm', name=name)
        thread.participants.set([request.user, u])
        dm_threads.append((u, thread))

    thread_id = request.GET.get('thread')
    dm_user_id = request.GET.get('dm')
    current_thread = group_thread

    if dm_user_id:
        try:
            other = User.objects.get(id=dm_user_id)
            name = f"dm-{min(other.id, request.user.id)}-{max(other.id, request.user.id)}"
            current_thread, _ = ChatThread.objects.get_or_create(thread_type='dm', name=name)
            current_thread.participants.set([request.user, other])
        except User.DoesNotExist:
            current_thread = group_thread
    elif thread_id:
        try:
            current_thread = ChatThread.objects.get(id=thread_id)
        except ChatThread.DoesNotExist:
            current_thread = group_thread

    if not _chat_user_can_access_thread(request.user, current_thread):
        current_thread = group_thread

    current_dm_user = None
    if current_thread.thread_type == 'dm':
        current_dm_user = current_thread.participants.exclude(id=request.user.id).first()

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip().lower()
        if action in {'toggle_mute', 'set_mute'}:
            target_thread_id = request.POST.get('thread_id')
            try:
                target_thread = ChatThread.objects.get(id=target_thread_id)
            except ChatThread.DoesNotExist:
                return redirect('chat')
            if not _chat_user_can_access_thread(request.user, target_thread):
                return redirect('chat')

            current_mute = _chat_get_active_mute(request.user, target_thread)
            ChatThreadMute.objects.filter(user=request.user, thread=target_thread).delete()

            if action == 'toggle_mute':
                if not current_mute:
                    ChatThreadMute.objects.create(user=request.user, thread=target_thread, muted_until=None)
            else:
                duration = (request.POST.get('mute_duration') or '').strip().lower()
                if duration in {'', '0', 'off', 'none', 'unmute'}:
                    pass
                elif duration == 'forever':
                    ChatThreadMute.objects.create(user=request.user, thread=target_thread, muted_until=None)
                else:
                    hours_map = {'1h': 1, '8h': 8, '24h': 24}
                    hours = hours_map.get(duration)
                    if hours:
                        ChatThreadMute.objects.create(
                            user=request.user,
                            thread=target_thread,
                            muted_until=timezone.now() + timedelta(hours=hours),
                        )
            return redirect(_chat_thread_url(target_thread.id))

        content = (request.POST.get('content') or '').strip()
        file = request.FILES.get('file')
        target_thread_id = request.POST.get('thread_id') or current_thread.id
        try:
            current_thread = ChatThread.objects.get(id=target_thread_id)
        except ChatThread.DoesNotExist:
            current_thread = group_thread
        if not _chat_user_can_access_thread(request.user, current_thread):
            current_thread = group_thread

        if content or file:
            msg = ChatMessage.objects.create(sender=request.user, content=content or '', file=file, thread=current_thread)
            try:
                _chat_mark_thread_read(request.user, current_thread, msg.created_at)
            except DatabaseError:
                pass

            try:
                recipient_ids = _chat_recipients_ids(current_thread, request.user.id)
                muted_ids = set(
                    ChatThreadMute.objects.filter(thread=current_thread, user_id__in=recipient_ids)
                    .filter(_chat_active_mute_q())
                    .values_list('user_id', flat=True)
                )
                notif_user_ids = [uid for uid in recipient_ids if uid not in muted_ids]
                notif_targets = User.objects.filter(id__in=notif_user_ids, is_active=True).only('id')
                preview = (msg.content or '').strip()
                if preview:
                    preview = preview[:60] + ('…' if len(preview) > 60 else '')
                elif msg.file:
                    preview = "a trimis un fișier"
                else:
                    preview = "mesaj nou"
                text = f"{msg.sender.username}: {preview}"
                Notification.objects.bulk_create([
                    Notification(
                        user=u,
                        message=text[:255],
                        url=_chat_thread_url(current_thread.id),
                        notif_type='chat',
                    )
                    for u in notif_targets
                ])
            except DatabaseError:
                pass

            try:
                ChatTypingState.objects.filter(user=request.user, thread=current_thread).delete()
            except DatabaseError:
                pass
        return redirect(_chat_thread_url(current_thread.id))

    chat_messages = list(
        ChatMessage.objects.select_related('sender', 'thread').filter(thread=current_thread).order_by('created_at')
    )
    try:
        _chat_mark_thread_read(request.user, current_thread, timezone.now())
        Notification.objects.filter(
            user=request.user,
            is_read=False,
            notif_type='chat',
            url=_chat_thread_url(current_thread.id),
        ).update(is_read=True)
    except DatabaseError:
        pass

    recipient_ids_for_status = _chat_recipients_ids(current_thread, request.user.id)
    other_reads = {
        st.user_id: st.last_read_at
        for st in ChatThreadReadState.objects.filter(thread=current_thread).exclude(user_id=request.user.id)
    }
    for msg in chat_messages:
        if msg.sender_id != request.user.id:
            continue
        seen_count = 0
        for uid in recipient_ids_for_status:
            seen_at = other_reads.get(uid)
            if seen_at and seen_at >= msg.created_at:
                seen_count += 1
        if current_thread.thread_type == 'dm':
            msg.read_label = 'Seen' if seen_count > 0 else 'Delivered'
        else:
            total = len(recipient_ids_for_status)
            if total and seen_count >= total:
                msg.read_label = 'Seen by all'
            elif seen_count > 0:
                msg.read_label = f'Seen by {seen_count}'
            else:
                msg.read_label = 'Delivered'

    unread_rows = Notification.objects.filter(
        user=request.user,
        is_read=False,
        notif_type='chat',
        url__startswith=f"{reverse('chat')}?thread=",
    ).values('url').annotate(total=models.Count('id'))
    unread_by_url = {row['url']: row['total'] for row in unread_rows}

    def thread_info(label, thread, is_group, other_user=None):
        last_msg = thread.messages.order_by('-created_at').first()
        last_time = timezone.localtime(last_msg.created_at) if last_msg else None
        if last_msg:
            if last_msg.content:
                last_text = last_msg.content[:40] + ('…' if len(last_msg.content) > 40 else '')
            elif last_msg.file:
                fname = last_msg.file.name.split('/')[-1]
                last_text = f"📎 {fname}"
            else:
                last_text = ''
        else:
            last_text = ''
        unread_count = unread_by_url.get(_chat_thread_url(thread.id), 0)
        return {
            'label': label,
            'thread': thread,
            'is_group': is_group,
            'last_time': last_time,
            'last_text': last_text,
            'unread': unread_count,
            'online': _chat_dm_online(other_user) if other_user else False,
            'user_id': other_user.id if other_user else None,
            'email': other_user.email if other_user else '',
        }

    thread_items = [thread_info(group_thread.name or 'Team Chat', group_thread, True)]
    thread_items += [thread_info(u.username, t, False, other_user=u) for u, t in dm_threads]
    tz = timezone.get_current_timezone()
    thread_items.sort(key=lambda x: x['last_time'] or timezone.datetime.min.replace(tzinfo=tz), reverse=True)

    try:
        prof = request.user.profile
        prof.last_seen = timezone.now()
        prof.save(update_fields=['last_seen'])
    except Exception:
        pass

    active_mute = _chat_get_active_mute(request.user, current_thread)
    thread_mute_value, thread_mute_note = _chat_mute_value_and_note(active_mute)

    typing_users = _chat_typing_users(current_thread, exclude_user_id=request.user.id)
    if typing_users:
        names = [f"@{item['username']}" for item in typing_users[:2]]
        if len(typing_users) == 1:
            current_status_text = f"{names[0]} is typing..."
        else:
            current_status_text = f"{', '.join(names)} are typing..."
    elif current_thread.thread_type == 'dm' and current_dm_user:
        current_status_text = 'Online' if _chat_dm_online(current_dm_user) else 'Offline'
    else:
        current_status_text = ''

    user_directory = list(User.objects.values('id', 'username', 'email'))
    context = {
        'chat_messages': chat_messages,
        'emoji_list': emoji_list,
        'threads': thread_items,
        'current_thread': current_thread,
        'current_dm_user': current_dm_user,
        'current_dm_online': _chat_dm_online(current_dm_user),
        'user_directory': user_directory,
        'thread_mute_value': thread_mute_value,
        'thread_mute_note': thread_mute_note,
        'current_status_text': current_status_text,
    }
    return render(request, 'core/chat.html', context)


@login_required
def chat_presence_api(request):
    thread_id = (request.POST.get('thread_id') or request.GET.get('thread_id') or '').strip()
    if not thread_id:
        return JsonResponse({'ok': False, 'error': 'missing_thread_id'}, status=400)

    try:
        thread = ChatThread.objects.get(id=thread_id)
    except ChatThread.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'thread_not_found'}, status=404)

    if not _chat_user_can_access_thread(request.user, thread):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    now_ts = timezone.now()
    try:
        ChatThreadMute.objects.filter(muted_until__isnull=False, muted_until__lte=now_ts).delete()
        ChatTypingState.objects.filter(last_typed_at__lt=now_ts - timedelta(minutes=5)).delete()
    except DatabaseError:
        pass

    if request.method == 'POST':
        typing_val = (request.POST.get('typing') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        try:
            _chat_mark_thread_read(request.user, thread, now_ts)
            if typing_val:
                ChatTypingState.objects.update_or_create(
                    user=request.user,
                    thread=thread,
                    defaults={'last_typed_at': now_ts},
                )
            else:
                ChatTypingState.objects.filter(user=request.user, thread=thread).delete()
            prof = getattr(request.user, 'profile', None)
            if prof is not None:
                prof.last_seen = now_ts
                prof.save(update_fields=['last_seen'])
        except DatabaseError:
            pass

    typing_users = _chat_typing_users(thread, exclude_user_id=request.user.id, now_ts=now_ts)
    read_states = ChatThreadReadState.objects.filter(thread=thread).exclude(user_id=request.user.id)
    read_payload = [
        {
            'user_id': row.user_id,
            'last_read_at': row.last_read_at.isoformat() if row.last_read_at else None,
        }
        for row in read_states
    ]
    recipients_payload = [{'user_id': uid} for uid in _chat_recipients_ids(thread, request.user.id)]

    dm_online = False
    if thread.thread_type == 'dm':
        dm_user = thread.participants.exclude(id=request.user.id).first()
        dm_online = _chat_dm_online(dm_user)

    return JsonResponse({
        'ok': True,
        'typing': typing_users,
        'read_states': read_payload,
        'recipients': recipients_payload,
        'dm_online': dm_online,
        'thread_type': thread.thread_type,
    })


def _is_admin_user(user):
    fn = ''
    try:
        fn = (user.profile.function or '').lower()
    except Exception:
        fn = ''
    admin_labels = {'admin', 'administrator', 'manager', 'owner'}
    return user.is_superuser or user.is_staff or fn in admin_labels


def _working_days_between(start_dt, end_dt):
    if not start_dt or not end_dt:
        return 0
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    span_days = (end_dt - start_dt).days + 1
    full_weeks, extra = divmod(span_days, 7)
    days = full_weeks * 5
    start_wd = start_dt.weekday()
    for i in range(extra):
        if (start_wd + i) % 7 < 5:
            days += 1
    return days


def _leave_balance_for_users(users, year=None):
    current_year = year or timezone.localdate().year
    year_start = date(current_year, 1, 1)
    year_end = date(current_year, 12, 31)
    user_list = list(users)
    balances = {}

    for u in user_list:
        prof = getattr(u, 'profile', None)
        try:
            allocated = int(getattr(prof, 'annual_leave_days', ANNUAL_LEAVE_DEFAULT_DAYS) or ANNUAL_LEAVE_DEFAULT_DAYS)
        except Exception:
            allocated = ANNUAL_LEAVE_DEFAULT_DAYS
        allocated = max(allocated, 0)
        balances[u.id] = {
            'year': current_year,
            'allocated': allocated,
            'approved': 0,
            'pending': 0,
            'remaining': allocated,
            'requestable': allocated,
        }

    if not user_list:
        return balances

    reqs = HolidayRequest.objects.filter(
        user_id__in=[u.id for u in user_list],
        holiday_type='annual',
        start_date__lte=year_end,
        end_date__gte=year_start,
    ).only('user_id', 'status', 'start_date', 'end_date')

    for req in reqs:
        if req.user_id not in balances:
            continue
        start_dt = max(req.start_date, year_start)
        end_dt = min(req.end_date, year_end)
        days = _working_days_between(start_dt, end_dt)
        if req.status == 'approved':
            balances[req.user_id]['approved'] += days
        elif req.status == 'pending':
            balances[req.user_id]['pending'] += days

    for data in balances.values():
        data['remaining'] = max(data['allocated'] - data['approved'], 0)
        data['requestable'] = max(data['allocated'] - data['approved'] - data['pending'], 0)

    return balances


def _leave_balance_for_user(user, year=None):
    return _leave_balance_for_users([user], year).get(user.id, {
        'year': year or timezone.localdate().year,
        'allocated': ANNUAL_LEAVE_DEFAULT_DAYS,
        'approved': 0,
        'pending': 0,
        'remaining': ANNUAL_LEAVE_DEFAULT_DAYS,
        'requestable': ANNUAL_LEAVE_DEFAULT_DAYS,
    })


@login_required
def calendar_worker(request):
    context = {
        'calendar_sync_url': _calendar_sync_url(request, request.user),
        'google_calendar_connected': _google_calendar_connected(request.user),
        'google_calendar_ready': _google_oauth_ready(),
    }
    if _is_admin_user(request.user):
        return render(request, 'core/calendar-admin.html', context)
    return render(request, 'core/calendar-worker.html', context)


@login_required
def calendar_info_worker(request):
    return render(request, 'core/calendar-info-worker.html')


@login_required
def calendar_info_admin(request):
    # folosește același template de info (versiune admin)
    return render(request, 'core/calendar-info-admin.html')


@login_required
def calendar_request_worker(request):
    import calendar
    my_requests = HolidayRequest.objects.filter(user=request.user).order_by('-created_at')
    leave_balance = _leave_balance_for_user(request.user)

    holiday_choices = HolidayRequest.HOLIDAY_TYPES

    today = timezone.localdate()
    # permite navigare prin query params ?y=2026&m=3
    try:
        year = int(request.GET.get('y', today.year))
        month = int(request.GET.get('m', today.month))
        if month < 1 or month > 12:
            year = today.year
            month = today.month
    except Exception:
        year = today.year
        month = today.month
    cal_rows = calendar.monthcalendar(year, month)
    month_name = calendar.month_name[month]

    # calculează lunile precedente/următoare pentru butoanele nav
    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1
    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    start_sel = end_sel = None
    holiday_error = None

    if request.method == 'POST':
        holiday_type = request.POST.get('holiday_type') or 'annual'
        start_date = request.POST.get('start_date')
        end_date = request.POST.get('end_date')
        comment = (request.POST.get('comment') or '').strip()
        if start_date and end_date:
            try:
                start_sel = timezone.datetime.fromisoformat(start_date).date()
                end_sel = timezone.datetime.fromisoformat(end_date).date()
            except Exception:
                start_sel = end_sel = None
                holiday_error = "Datele selectate nu sunt valide."

            if not holiday_error and start_sel and end_sel and end_sel < start_sel:
                holiday_error = "Data de final trebuie să fie după data de început."

            if not holiday_error and holiday_type == 'annual':
                requested_days = _working_days_between(start_sel, end_sel)
                if requested_days <= 0:
                    holiday_error = "Pentru concediu anual selectează cel puțin o zi lucrătoare."
                elif requested_days > leave_balance['requestable']:
                    holiday_error = (
                        f"Nu ai suficiente zile disponibile. "
                        f"Disponibile pentru cerere: {leave_balance['requestable']}."
                    )

            if not holiday_error:
                HolidayRequest.objects.create(
                    user=request.user,
                    holiday_type=holiday_type,
                    start_date=start_date,
                    end_date=end_date,
                    comment=comment,
                    status='pending'
                )
                return redirect('calendar_request_worker')
        # keep form values
        if start_sel is None or end_sel is None:
            try:
                start_sel = timezone.datetime.fromisoformat(start_date).date() if start_date else None
                end_sel = timezone.datetime.fromisoformat(end_date).date() if end_date else None
            except Exception:
                start_sel = end_sel = None

    context = {
        'my_requests': my_requests,
        'leave_balance': leave_balance,
        'holiday_error': holiday_error,
        'month_rows': cal_rows,
        'month_name': month_name,
        'year': year,
        'month': month,
        'today_day': today.day,
        'start_sel': start_sel,
        'end_sel': end_sel,
        'holiday_choices': holiday_choices,
        'holiday_type': request.POST.get('holiday_type') if request.method == 'POST' else None,
        'prev_y': prev_year,
        'prev_m': prev_month,
        'next_y': next_year,
        'next_m': next_month,
    }
    return render(request, 'core/calendar-worker-pending.html', context)


# Admin: tabel cereri
@login_required
def calendar_holiday_requests_admin(request):
    if not _is_admin_user(request.user):
        return redirect('calendar')
    requests_qs = HolidayRequest.objects.select_related('user', 'user__profile').order_by('-created_at')

    if request.method == 'POST':
        req_id = request.POST.get('req_id')
        action = request.POST.get('action')
        note = (request.POST.get('admin_note') or '').strip()
        if req_id and action in ['approved', 'rejected']:
            try:
                hr = HolidayRequest.objects.get(id=req_id)
                hr.status = action
                hr.admin_note = note
                hr.save(update_fields=['status', 'admin_note'])
            except HolidayRequest.DoesNotExist:
                pass
        return redirect('calendar_holiday_requests_admin')
    request_rows = list(requests_qs)
    balance_map = _leave_balance_for_users([r.user for r in request_rows])
    for row in request_rows:
        row.leave_balance = balance_map.get(row.user_id, _leave_balance_for_user(row.user))

    return render(request, 'core/calendar-holiday-requests-admin.html', {
        'requests': request_rows,
    })


@login_required
def export_holiday_requests_csv(request):
    if not _is_admin_user(request.user):
        return redirect('calendar')

    status = (request.GET.get('status') or '').strip().lower()
    valid_statuses = {k for k, _ in HolidayRequest.STATUS_CHOICES}
    qs = HolidayRequest.objects.select_related('user', 'user__profile').order_by('-created_at')
    if status in valid_statuses:
        qs = qs.filter(status=status)

    requests_list = list(qs)
    balances = _leave_balance_for_users([r.user for r in requests_list])

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    filename = f"holiday_requests_{timezone.localdate().isoformat()}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow([
        'Request ID',
        'User',
        'Email',
        'Function',
        'Holiday Type',
        'Start Date',
        'End Date',
        'Working Days',
        'Status',
        'Comment',
        'Admin Note',
        'Year',
        'Allocated Days',
        'Approved Days',
        'Pending Days',
        'Remaining Days',
        'Created At',
    ])

    for r in requests_list:
        prof = getattr(r.user, 'profile', None)
        role = (prof.function or '') if prof else ''
        b = balances.get(r.user_id, _leave_balance_for_user(r.user))
        writer.writerow([
            r.id,
            r.user.get_full_name() or r.user.username,
            r.user.email,
            role,
            r.get_holiday_type_display(),
            r.start_date.isoformat(),
            r.end_date.isoformat(),
            _working_days_between(r.start_date, r.end_date),
            r.get_status_display(),
            (r.comment or '').replace('\r\n', '\n').replace('\r', '\n'),
            (r.admin_note or '').replace('\r\n', '\n').replace('\r', '\n'),
            b['year'],
            b['allocated'],
            b['approved'],
            b['pending'],
            b['remaining'],
            timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M'),
        ])
    return response


# Admin: listează concedii aprobate/curente
@login_required
def calendar_current_holidays_admin(request):
    if not _is_admin_user(request.user):
        return redirect('calendar')
    today = timezone.localdate()
    current = HolidayRequest.objects.select_related('user', 'user__profile').filter(
        status='approved',
        start_date__lte=today,
        end_date__gte=today
    ).order_by('start_date')
    soon_limit = today + timezone.timedelta(days=14)
    upcoming = HolidayRequest.objects.select_related('user', 'user__profile').filter(
        status='approved',
        start_date__gt=today,
        start_date__lte=soon_limit
    ).order_by('start_date')
    return render(request, 'core/calendar-see-holidays-request-admin.html', {
        'current_requests': current,
        'upcoming_requests': upcoming,
        'today': today,
    })
