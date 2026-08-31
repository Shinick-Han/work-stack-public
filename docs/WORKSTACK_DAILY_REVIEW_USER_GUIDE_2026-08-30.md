# Daily Review — user guide

## Record today’s execution evidence

1. Open Work Stack and choose **Daily Review** in the left navigation, or press `6` while
   focus is not inside an input.
2. Confirm **Review date**. You can select an earlier date, but not a future date.
3. Choose **Check in now** to record the local start time. **Update time** replaces the
   displayed start time with a new explicit check-in intent.
4. Under **New evidence**, choose the planning Task that the work belongs to.
5. Enter Done, Next, and Blockers with one item per line. At least one field needs an
   item; the other fields can remain empty.
6. Choose **Add review entry**. The Day record and Seven-day review refresh after the
   server confirms the entry.

## Recover from a lost response

If the page says the entry may have committed, keep the Task and all text unchanged and
choose **Retry unchanged entry**. Work Stack reuses the original intent identity, so an
already committed entry is returned rather than duplicated. Editing the Task or any text
starts a deliberately new intent.

Check-in behaves the same way: choose **Retry check-in** without changing the date to
verify an ambiguous first attempt.

## Read the review

- **Day record** lists each Task entry and its Done, Next, and Blocker facts.
- Choose a Task ID or title to open the authoritative planning detail.
- **Seven-day review** deduplicates facts per Task and shows the Objectives, active days,
  and counts that fall in the seven-day window ending on the selected date.

Daily Review never changes Task status automatically. It does not start a Conduit agent,
create a Taskroom, send a Teams message, or send Outlook mail.
