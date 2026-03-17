from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=100, blank=True)
    # ex: Front-end Developer
    function = models.CharField(max_length=100, blank=True)
    annual_leave_days = models.PositiveIntegerField(default=28)
    calendar_feed_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    last_seen = models.DateTimeField(blank=True, null=True)
    about = models.TextField(blank=True)
    languages = models.TextField(blank=True)
    skills = models.TextField(blank=True)
    experience = models.TextField(blank=True)
    education = models.TextField(blank=True)

    def __str__(self):
        return self.full_name or self.user.username


class Task(models.Model):
    STATUS_CHOICES = [
        ('todo', 'To Do'),
        ('revision', 'In Revision'),
        ('done', 'Done'),
        ('archived', 'Archived'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='todo')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    due_date = models.DateField(blank=True, null=True)
    assignee = models.ForeignKey(User, related_name='assigned_tasks', on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(User, related_name='created_tasks', on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date', '-created_at']

    def __str__(self):
        return self.title


class TaskComment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='task_comments')
    body = models.TextField(max_length=600)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.author.username} on {self.task_id}"


class TaskReminderLog(models.Model):
    REMINDER_TYPES = [
        ('due_24h', 'Due in 24h'),
        ('due_today', 'Due today'),
        ('overdue', 'Overdue'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='task_reminder_logs')
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='reminder_logs')
    reminder_type = models.CharField(max_length=20, choices=REMINDER_TYPES)
    reminder_date = models.DateField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('user', 'task', 'reminder_type', 'reminder_date')]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} {self.reminder_type} {self.task_id} {self.reminder_date}"


class ChatThread(models.Model):
    THREAD_TYPES = (
        ('group', 'Group'),
        ('dm', 'Direct'),
    )
    thread_type = models.CharField(max_length=10, choices=THREAD_TYPES, default='group')
    name = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_threads', null=True, blank=True)
    participants = models.ManyToManyField(User, related_name='chat_threads', blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('thread_type', 'name')]

    def __str__(self):
        if self.thread_type == 'group':
            return self.name or 'Group'
        return f"DM: {', '.join(self.participants.values_list('username', flat=True))}"


class ChatMessage(models.Model):
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='messages', null=True, blank=True)
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    content = models.TextField()
    file = models.FileField(upload_to='chat_files/', blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.sender.username}: {self.content[:30]}"


class ChatThreadMute(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='muted_threads')
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='muted_by')
    muted_until = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('user', 'thread')]


class ChatThreadReadState(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_read_states')
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='read_states')
    last_read_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'thread')]


class ChatTypingState(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_typing_states')
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='typing_states')
    last_typed_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'thread')]


class Meeting(models.Model):
    title = models.CharField(max_length=200)
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    participants = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    google_event_id = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_meetings')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['date', 'time']

    def __str__(self):
        return f"{self.title} @ {self.date}"


class Submission(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='submissions')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submissions')
    description = models.TextField(blank=True)
    file = models.FileField(upload_to='submissions/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewer_comment = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Submission for {self.task.title} by {self.author.username}"


class Notification(models.Model):
    NOTIF_TYPES = [
        ('chat', 'Chat'),
        ('task', 'Task'),
        ('holiday', 'Holiday'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    url = models.CharField(max_length=255, blank=True)
    notif_type = models.CharField(max_length=20, choices=NOTIF_TYPES, default='other')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username}: {self.message[:40]}"


class HolidayRequest(models.Model):
    HOLIDAY_TYPES = [
        ('annual', 'Concediu de odihnă anual'),
        ('medical', 'Concediu medical'),
        ('maternity', 'Concediu de maternitate'),
        ('paternal', 'Concediu paternal'),
        ('study', 'Concediu de studii'),
        ('partial_child', 'Concediu parțial plătit pentru îngrijirea copilului'),
        ('supp_unpaid_child', 'Concediu suplimentar neplătit pentru îngrijirea copilului'),
        ('care_family', 'Concediu pentru îngrijirea unui membru bolnav al familiei'),
        ('child_disability', 'Concediu pentru îngrijirea copilului cu dizabilități'),
        ('unpaid_personal', 'Concediu neplătit din cont propriu'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='holiday_requests')
    holiday_type = models.CharField(max_length=20, choices=HOLIDAY_TYPES, default='annual')
    start_date = models.DateField()
    end_date = models.DateField()
    comment = models.TextField(blank=True)
    admin_note = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} {self.holiday_type} {self.start_date} - {self.end_date}"


class GoogleCalendarConnection(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_calendar_connection')
    refresh_token = models.TextField()
    access_token = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    calendar_id = models.CharField(max_length=120, default='primary')
    sync_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"GoogleCalendarConnection<{self.user.username}>"


class BotReminder(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bot_reminders')
    message = models.CharField(max_length=255)
    remind_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-remind_at']

    def __str__(self):
        return f"BotReminder<{self.user.username} @ {self.remind_at}>"


class BotMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bot_messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"BotMessage<{self.user.username} {self.role}>"
