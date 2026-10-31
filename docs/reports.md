# Scheduled reports

Subscribe to a [saved view](lists.md#saved-views) and get it as a CSV by email every day,
week or month. Open a saved view on a list page and choose **Subscribe**; your reports
are under **System → My reports**.

```python
VANTAGE = {
    "REPORTS_MAX_ATTACHMENT_MB": 10,   # past this the email is a short note, no file
    "REPORTS_BASE_URL": "https://panel.example.com",
}
```

Nothing is sent until you run the sender. Put this in cron, every minute:

```console
$ python manage.py vantage_reports
Queued 2 due reports.
Removed 14 old send logs.
```

It also needs a working email setup: reports go out through Django's email backend
(`EMAIL_BACKEND` and `DEFAULT_FROM_EMAIL`, or `MAILERS` where your Django has it), the same way every
other message from your project does.

## Subscribing

Choose a saved view (a report is always a saved view: that is what gives it a name and a
filter) and **Subscribe**:

| | |
|---|---|
| How often | every day, every week (pick the weekday), or every month (pick the day) |
| Time | the time of day to send, in the site's time zone (`TIME_ZONE`) |
| Send to me | on by default; goes to the address on your account |
| Also send to | other addresses, comma separated. Only shown to people holding `vantage.send_reports_to_others` |

A monthly report on the 31st sends on the last day of shorter months. "08:00" stays 08:00
when the clocks change. The same form edits a subscription later, from **My reports**.

You can subscribe to a view you can open: the model's `view` permission and a view that is
yours or shared with you. Anyone with an email address can mail a view to themselves;
sending to anyone else is a permission of its own, because a mailbox is not necessarily
the person's. `REPORTS_MAX_PER_USER` limits how many one person may hold.

## Whose permissions the report runs with

**The export runs as the person who subscribed, with the rights they have when it
sends.** Not the ones they had when they subscribed: nothing about their access is stored,
the account is read from the database at send time, and the view is resolved by the same
code as the list page and its Export button. That means their model permission, their row
scoping, their tenant and the columns they have chosen for that list, and the same
masking: a sensitive column is exported as bullets, exactly as on the page.

A report is **skipped** and the reason recorded in the log, never sent, when:

* the account is inactive, or is no longer staff;
* two-factor sign-in is required of them (see [Two-factor](two-factor.md)) and they have
  not set it up, since the panel would make them enrol before showing them anything;
* the subscription was paused or deleted after a scheduled send had already been queued;
* they may no longer view the model;
* the saved view is no longer shared with them (or was made private);
* a filter in the view names a field that no longer exists. The list page shows a banner
  and carries on with more rows; an inbox has no banner, so the report is held back
  rather than silently widened;
* the model is no longer in the panel, the tenant has changed, or there is nobody left to
  send to.

Other addresses are checked the same way: they receive the report only while the
subscriber *still* holds `send_reports_to_others`. If they lost it, the report goes to the
subscriber alone and the log says the others were left out.

A skipped report keeps its schedule, so it sends again once access is restored.

**Send now** is an explicit request from the owner or a manager, so it still sends a
*paused* report; every other check above applies to it. It is refused in read-only mode,
like Pause and Resume, and each report can be sent this way at most once a minute: a
second click inside that minute is told how long to wait and queues nothing.

## The email

One message per recipient, so nobody learns who else receives it, with the CSV attached
and the number of rows in the body. Every message carries a link to the saved view in the
panel and an **unsubscribe** link, plus `List-Unsubscribe` headers for one-click
unsubscribe in mail clients.

**Size cap.** Above `REPORTS_MAX_ATTACHMENT_MB` of CSV (default 10) the file is not
attached. The email says how many rows there were, that the file was over the limit, and
points at the saved view, where Export has no such cap. The log marks the send as sent
without the file. Mind your mail provider's own limit, which is usually lower once the
attachment is encoded.

The links need the panel's address. `REPORTS_BASE_URL` sets it; without it the address the
subscription was made from is used. A subscription with neither is skipped rather than
sent without an unsubscribe link.

**Set `REPORTS_BASE_URL` in production.** The fallback address is read from the `Host`
header of the request that created the subscription, and a client chooses that header. If
the proxy in front of Django does not pin it (or `ALLOWED_HOSTS` is loose), whoever
subscribes can decide where every later email's unsubscribe and panel links point.
`manage.py check --deploy` warns about this as `vantage.W115`.

## Unsubscribing

The link is signed with `SECRET_KEY` and names one subscription. It needs no sign-in,
and all it can do is **pause** that subscription: the owner still sees it under My
reports, with the reason, and can resume it. Opening the link changes nothing (mail
scanners open every link); the page has a button, and mail clients' one-click `POST` does
the same. A link keeps working when `REPORTS` is off, and does not expire.

Rotating `SECRET_KEY` invalidates links in mail already sent, unless the old key is in
`SECRET_KEY_FALLBACKS`.

## The pages

**My reports** lists your subscriptions: schedule, next and last send, and a log of every
send with its status (queued, running, sent, skipped, failed), the row count, the
recipients reached, how long it took and the reason or error. Each row has **Send now**,
**Pause** or **Resume**, **Edit** and **Delete**.

**System → Scheduled reports** is the same for everyone's, for superusers and anyone
holding `vantage.manage_reports`. Managers can pause, resume, delete and send now. They
cannot edit: what is sent, and to whom, is the owner's, because the report runs as them.
**Send now** runs as the subscriber and sends to the subscriber's recipients, not to the
manager. It leaves the schedule alone.

`VANTAGE["READ_ONLY"]` blocks creating, editing, pausing and deleting. `VANTAGE["REPORTS"]
= False` hides the pages and the Subscribe control and stops sending; subscriptions and
logs are kept.

## How sending is scheduled

`manage.py vantage_reports` finds the active subscriptions whose time has come and
**claims** each one by moving its next time forward with a conditional update. Only the
process that changed the row queues a send, so running the command twice in the same
minute, or two cron hosts at once, sends once.

* A server that was down for a week sends **one** report when it returns, not seven. The
  next time is worked out from now.
* Each send then goes through the same backend detection as
  [background jobs](lists.md#background-jobs) and [webhooks](webhooks.md): `django.tasks`
  with a real queue, Celery with a broker, or RQ. A Celery worker needs `vantage.reports`
  in its `imports` alongside `vantage.jobs`. **With none**, the command sends the emails
  itself, one after another, and the pages say so.
* A queued send nobody picked up for five minutes is queued again; a run stuck "running"
  for an hour is closed as failed. Both are done by the command.
* Send logs older than `REPORTS_RETENTION_DAYS` are removed by the command;
  `--no-sweep` skips that.

Every minute is the right cadence for cron: a report goes out within a minute of its time.

## Settings

See [Settings](settings.md#scheduled-reports). A malformed value is `vantage.E115` (a
switch that is not `True`/`False`), `vantage.E116` (a limit that is not a number above
zero) or `vantage.E117` (a `REPORTS_BASE_URL` that does not start with `http://` or
`https://`).

## Permissions

| Permission | Allows |
|---|---|
| `vantage.manage_reports` | The System page: pause, resume, delete and send anyone's reports |
| `vantage.send_reports_to_others` | Naming addresses other than your own |

Superusers hold both.

## Known limits

* **CSV only**, with the columns the subscriber has on that list. No Excel or JSON.
* The CSV is built in memory up to the size cap, so a report is bounded by
  `REPORTS_MAX_ATTACHMENT_MB`; the rows are read in chunks and building stops as soon as
  the cap is passed.
* A text cell that begins with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
  `'`, exactly as in the Export button, so a spreadsheet shows it as text instead of running
  it as a formula. Numbers, dates and booleans are not changed.
* A view whose list mode is a board or a map exports the table's columns.
* Deleting a saved view deletes the subscriptions to it.
* There is no per-recipient unsubscribe: the link pauses the subscription for everyone it
  goes to. The owner decides who is on it.
* Without a task backend, the emails are sent by the process that queued them, so the
  cron job (or, for Send now, the request) waits until they are out.
