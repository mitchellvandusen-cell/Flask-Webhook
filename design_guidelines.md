# Design Guidelines - Flask Webhook API

## Project Classification
This is a **backend API service** without a user-facing frontend interface. It's a webhook endpoint that processes data programmatically.

## No Visual Design Required
This project does not require traditional web design guidelines as it:
- Has no user interface
- No pages or layouts
- No visual components
- Functions as a pure API endpoint

## API Design Specifications

### Response Structure
**JSON Response Format:**
```
{
  "status": "success" | "error",
  "reply": "AI-generated NEPQ response text",
  "metadata": {
    "processed_at": "ISO timestamp",
    "recipient": "first_name from request"
  }
}
```

### Error Response Format
```
{
  "status": "error",
  "error": "Error description",
  "code": "ERROR_CODE"
}
```

## Logging Interface (If Implemented)

If you choose to add a simple web interface for monitoring webhook activity:

**Typography:**
- Use system fonts (sans-serif stack)
- Monospace font for JSON/log data display

**Layout:**
- Single-page dashboard
- Reverse chronological log entries
- Max-width container: 1200px
- Spacing: Tailwind units of 4, 6, and 8

**Components:**
- Log entry cards with timestamp, request data, and response
- Status indicators (success/error badges)
- Search/filter functionality for logs
- Code blocks with syntax highlighting for JSON

**Color-Neutral Structure:**
- Focus on data readability
- Clear visual hierarchy between request/response pairs
- Adequate spacing between log entries (py-6)

## Priority Focus
The primary focus should be on:
1. Robust error handling
2. Clear API documentation
3. Request/response validation
4. Secure API key management via Replit integration
5. Comprehensive logging for debugging

No visual design work is required for the core webhook functionality.