# Reporting security vulnerabilities

Report suspected vulnerabilities privately through **Security → Report a vulnerability** in the GitHub repository. If private reporting is unavailable, ask a maintainer for a private channel without sharing vulnerability details in a public issue or pull request.

Include the affected version or commit, the conditions needed to reproduce the issue, and the impact you observed. Use synthetic data and redact any bot tokens, Telegram IDs, contact details, chat text, and database contents from reports and attachments.

If a bot token is exposed, revoke it through BotFather, set the replacement in the deployment's private configuration, and restart the bot. Restrict access to SQLite files and their snapshots. They can contain profiles, bookings, and the notification queue.
