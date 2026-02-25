# Bible AI - Data Retention & Audit Log Improvements

## Summary of Changes

This update implements comprehensive data retention and audit logging features to ensure:
1. **User ID Retention**: First-time sign-ins are tracked and the original user ID is always preserved
2. **Complete Audit Trail**: All user activities (logins, likes, saves, comments) are logged with full user details
3. **Data Persistence**: User data (comments, likes, saves, etc.) is never deleted and can be retrieved

## New Database Tables

### 1. `user_activity_logs`
Stores comprehensive activity records for every user action.

**Columns:**
- `id` - Primary key
- `user_id` - User's internal ID
- `google_id` - User's Google ID (for tracking across sessions)
- `email` - User's email address
- `action` - Type of action (e.g., USER_LOGIN, USER_LIKE, USER_COMMENT)
- `details` - JSON with additional context
- `ip_address` - Client IP address
- `user_agent` - Client browser/user agent
- `timestamp` - When the action occurred

### 2. `user_signup_logs`
Tracks first-time signups and enforces ID retention.

**Columns:**
- `id` - Primary key
- `user_id` - Original user ID (enforced on all subsequent logins)
- `google_id` - Google account ID (unique)
- `email` - User's email
- `name` - User's display name
- `first_signup_at` - When the user first signed up
- `last_login_at` - Most recent login time
- `signup_ip` - IP address of first signup
- `total_logins` - Running count of logins

## Modified Functions

### 1. `callback()` (OAuth Login Handler)
- **New Behavior**: 
  - Tracks first-time signups in `user_signup_logs`
  - Enforces original user ID retention on subsequent logins
  - Logs both `USER_SIGNUP` and `USER_LOGIN` events
  - Captures IP address and user agent

### 2. `log_user_activity()` 
- **Enhanced** to:
  - Write to both `audit_logs` (admin dashboard) and `user_activity_logs` (comprehensive tracking)
  - Fetch and store user's `google_id`, `email`, and `name`
  - Capture IP address and user agent for every action
  - Include full user details in the log payload

### 3. `like_verse()`
- **Added Logging**: 
  - `USER_LIKE` when a verse is liked
  - `USER_UNLIKE` when a verse is unliked

### 4. `save_verse()`
- **Added Logging**:
  - `USER_SAVE` when a verse is saved
  - `USER_UNSAVE` when a verse is unsaved

### 5. `post_comment()` (Community Messages)
- Already had logging, verified working with enhanced `log_user_activity()`

### 6. `init_db()` and `migrate_db()`
- **Added**: Automatic creation of `user_activity_logs` and `user_signup_logs` tables

## New API Endpoints

### 1. `GET /api/user_activity`
Retrieve the logged-in user's complete activity history.

**Query Parameters:**
- `limit` - Max records to return (default: 100, max: 500)
- `offset` - Pagination offset
- `action` - Filter by action type (optional)

**Response:**
```json
{
  "activities": [
    {
      "id": 1,
      "action": "USER_LOGIN",
      "details": {"message": "User login", ...},
      "ip_address": "192.168.1.1",
      "timestamp": "2024-01-01T12:00:00"
    }
  ],
  "total": 150,
  "limit": 100,
  "offset": 0
}
```

### 2. `GET /api/user_signup_info`
Retrieve user's original signup information for ID retention verification.

**Response:**
```json
{
  "user_id": 123,
  "google_id": "google_xxx",
  "email": "user@example.com",
  "name": "John Doe",
  "first_signup_at": "2024-01-01T10:00:00",
  "last_login_at": "2024-01-15T08:30:00",
  "signup_ip": "192.168.1.1",
  "total_logins": 15,
  "id_retained": true
}
```

### 3. `GET /api/user_data_summary`
Get a summary of all user data across all tables.

**Response:**
```json
{
  "user_id": 123,
  "data_retained": {
    "likes": 45,
    "saves": 23,
    "comments": 12,
    "community_messages": 8,
    "comment_replies": 5,
    "daily_actions": 89,
    "user_activity_logs": 182,
    "collections": 3
  },
  "total_records": 367,
  "data_retention_active": true
}
```

## Tracked User Actions

The following actions are now logged with full details:

| Action | Description |
|--------|-------------|
| `USER_SIGNUP` | New user account created |
| `USER_LOGIN` | User logged in |
| `USER_LIKE` | User liked a verse |
| `USER_UNLIKE` | User unliked a verse |
| `USER_SAVE` | User saved a verse |
| `USER_UNSAVE` | User unsaved a verse |
| `USER_COMMENT` | User posted a comment on a verse |
| `USER_COMMUNITY` | User posted a community message |

## Data Retention Guarantees

1. **User ID Persistence**: Once a user signs up, their `user_id` is permanently linked to their `google_id` in `user_signup_logs`. Even if the main `users` table were reset, the original ID mapping is preserved.

2. **Activity History**: Every action a user takes is logged in `user_activity_logs` with their `google_id` and `email`, creating a permanent audit trail.

3. **Content Preservation**: Comments, likes, saves, and community messages are never deleted (they may be soft-marked as deleted but remain in the database).

4. **Cross-Reference**: All tables link back to the original `user_id`, and the `user_signup_logs` table provides the immutable mapping between `google_id` and `user_id`.

## Migration Notes

When the application starts:
1. `migrate_db()` automatically creates the new tables if they don't exist
2. Existing users will have signup logs created on their next login
3. All existing data remains intact
4. New activity logging begins immediately

## Security Considerations

- IP addresses and user agents are logged for audit purposes
- Email addresses are stored in activity logs for user identification
- Google IDs are used as the immutable identifier
- All activity logs include timestamps for forensic analysis
