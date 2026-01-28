# 🎭 Re-Engagement Mode - Pattern Interrupt After 6+ Unanswered Messages

## Overview

When the bot has sent **6 or more consecutive messages** without getting a response from the lead, it automatically switches from "business mode" to "re-engagement mode" with humor and humanity.

## Problem It Solves

**Traditional bot behavior:**
```
Bot: "Hey, interested in life insurance?"
Bot: "Just following up on the coverage options"
Bot: "Did you have any questions about policies?"
Bot: "I wanted to check if you're still interested"
Bot: "Let me know if you'd like to discuss coverage"
Bot: "Circling back on those insurance options"
Bot: "Are you still looking for coverage?"
Lead: [crickets] 🦗
```

**Result:** Lead feels spammed, annoyed, and tunes out completely.

## New Behavior (Re-Engagement Mode)

After 6+ consecutive bot messages, the system detects radio silence and switches strategy:

```
Bot: "Hey, interested in life insurance?"
Bot: "Just following up on the coverage options"
Bot: "Did you have any questions about policies?"
Bot: "I wanted to check if you're still interested"
Bot: "Let me know if you'd like to discuss coverage"
Bot: "Circling back on those insurance options"
Bot: 🎭 "Okay I've sent you 6 messages and haven't heard back, feeling like I'm alone on the Titanic here 😅 You still interested or should I circle back later?"
Lead: "Haha sorry been busy, yeah let's talk"
```

**Result:** Pattern interrupt, human moment, re-engages the lead.

## How It Works

### 1. Detection

**Function:** `count_consecutive_bot_messages(recent_exchanges)`

Counts backward through conversation history to find consecutive bot messages without lead responses:

```python
def count_consecutive_bot_messages(recent_exchanges: list) -> int:
    consecutive_bot = 0
    for exchange in reversed(recent_exchanges):
        if exchange.get("role") == "bot":
            consecutive_bot += 1
        else:
            break  # Hit a lead message, stop counting
    return consecutive_bot
```

### 2. Activation

**Trigger:** `consecutive_bot_msgs >= 6`

When activated, special instructions are added to the AI prompt:

```
🎭 RE-ENGAGEMENT MODE (6+ unanswered messages):
- STOP selling insurance completely
- Be humorous, warm, and human (not business mode)
- Acknowledge you've been messaging without response
- Use ONE of these approaches:
  * Self-aware humor
  * Pattern interrupt
  * Dad joke (if male name)
  * Relatable moment
- Keep it light and friendly
- NO hard selling or pressure
- Goal: Get ANY response, even if it's "not interested"
```

### 3. Tone Shift

The bot goes from:
- ❌ Professional sales mode
- ❌ Feature/benefit focused
- ❌ Persistent follow-up

To:
- ✅ Human and self-aware
- ✅ Humorous and light
- ✅ Permission-based ("should I circle back?")

## Re-Engagement Strategies

The AI is instructed to **be creative** and come up with its own approach. No templates - the LLM creates original humor.

### General Approaches (AI chooses and creates):

**1. Self-Aware Humor**
- Acknowledge being ignored in a funny way
- Be self-deprecating about the situation
- Keep it light and relatable

**2. Pattern Interrupt**
- Stop talking business completely
- Ask about their life instead
- Show genuine human interest

**3. Dad Jokes (Male Names)**
- Come up with an original dad joke
- Keep it clean and harmless
- Follow with "still interested or should I circle back?"

**Note:** The AI generates its own jokes, not from templates. The LLM is creative.

**4. Relatable Moments**
- Acknowledge life gets busy
- Give them permission to say "not now"
- Offer to circle back later

The AI picks what feels most natural for the specific contact and creates something original.

## Examples

**Note:** These are illustrative examples. The actual bot creates its own original humor and responses - these are NOT templates.

### Example 1: AI Creates Self-Aware Humor

**Conversation:**
```
Bot: "Hey John! Interested in updating your life insurance?"
Bot: "Just following up on coverage options"
Bot: "Did you have any questions about policies?"
Bot: "Let me know if you'd like to discuss this"
Bot: "Circling back on those insurance options"
Bot: "Are you still looking for coverage?"
Bot: 🎭 [AI creates something original, e.g.] "John, I've basically written you a novel at this point 😅 Still interested or should I give you some space?"
```

**Lead responds:**
```
Lead: "haha sorry man been crazy busy, yeah let's chat"
Bot: [RETURNS TO NORMAL MODE] "No worries! When works for you - I've got slots tomorrow at 2pm or Friday at 10am?"
```

### Example 2: AI Creates Dad Joke

**Conversation:**
```
Bot: "Hey Mike! Quick question about life insurance"
Bot: "Following up on coverage"
Bot: "Did you have questions?"
Bot: "Let me know if interested"
Bot: "Circling back"
Bot: "Still interested?"
Bot: 🎭 [AI comes up with original dad joke] "Mike, I'll hit you with a dad joke: [creates original joke]. Anyway - you still thinking about this or should I bug you later?"
```

**Lead responds:**
```
Lead: "😂 that was bad but i laughed. yeah still interested"
Bot: [RETURNS TO NORMAL MODE] "Haha glad I could get a laugh! So what type of coverage were you thinking?"
```

### Example 3: AI Creates Permission-Based Exit

**Conversation:**
```
Bot: "Hey Sarah! Interested in life insurance?"
Bot: "Following up on options"
Bot: "Any questions?"
Bot: "Let me know"
Bot: "Circling back"
Bot: "Still thinking about it?"
Bot: 🎭 [AI creates original message] "Sarah - I can tell life's busy. No pressure. Good time to chat or should I try back later?"
```

**Lead responds:**
```
Lead: "thanks for checking, now's not good, maybe next month"
Bot: [RETURNS TO NORMAL MODE] "Totally understand! I'll check back with you next month. Take care!"
```

## Automatic Reset

Re-engagement mode **automatically deactivates** when:
- Lead sends ANY message (even "stop")
- Bot returns to normal conversation mode
- Counter resets to 0 consecutive bot messages

**No manual intervention needed** - the system self-corrects.

## Male Name Detection

The system maintains a list of 70+ common male first names to determine when dad jokes are appropriate:

```python
male_names = ["john", "mike", "david", "james", "robert", ...]
is_male_name = first_name.lower().strip() in male_names
```

If male name detected:
- ✅ Dad jokes are suggested as an option
- ✅ AI can choose to use humor targeting male audience

If NOT male name:
- ❌ Skip dad jokes
- ✅ Use other re-engagement strategies (humor, pattern interrupt, relatable)

## What Gets Logged

### When activated:
```
🎭 RE-ENGAGEMENT MODE ACTIVATED | 6 consecutive bot messages without response
```

### In the prompt (what AI sees):
```
🎭 RE-ENGAGEMENT MODE (6+ unanswered messages):
- STOP selling insurance completely
- Be humorous, warm, and human (not business mode)
- Consider a dad joke since contact appears to have a male name
```

### When lead responds:
```
[Mode automatically resets - no special log needed]
```

## Benefits

✅ **Pattern interrupt** - Breaks the monotony of sales messages
✅ **Human connection** - Shows self-awareness and humor
✅ **Permission-based** - Gives lead an easy out ("circle back later?")
✅ **Re-engagement** - Gets responses from silent leads
✅ **Automatic** - No manual intervention required
✅ **Context-aware** - Dad jokes only for male names

## Edge Cases

### Case 1: Lead Responds with "Stop"
```
Bot: 🎭 Re-engagement message
Lead: "stop messaging me"
Bot: "Understood, I'll stop reaching out. Take care!"
```

Mode resets, but lead's request is honored.

### Case 2: Booking Confirmed
```
Bot: Messages 1-5 (no response)
Bot: Message 6 (no response)
[Lead books appointment]
Bot: "Awesome! See you tomorrow at 2pm"
```

Re-engagement mode **does NOT activate** if booking was just made.

### Case 3: Lead Sends One-Word Reply
```
Bot: Messages 1-6
Bot: 🎭 "Feeling like I'm alone on the Titanic here 😅"
Lead: "ok"
Bot: [RETURNS TO NORMAL] "Great! When works for you?"
```

ANY lead message resets the counter.

### Case 4: Female Name Gets Dad Joke (Rare)
```
If first_name is "Alex" or "Jordan" (gender-neutral):
→ System might misidentify
→ Dad jokes could be used inappropriately
→ Not ideal, but not offensive (jokes are harmless)
```

**Mitigation:** The AI is instructed to "pick what fits best" - it won't force dad jokes if inappropriate.

## Configuration

**Threshold:** 6 consecutive bot messages (hardcoded)
**Can be adjusted in:** `tasks.py` line ~339: `if consecutive_bot_msgs >= 6`

**To change threshold:**
```python
# Line 339
if consecutive_bot_msgs >= 8:  # Increase to 8 messages
```

**To disable entirely:**
```python
# Line 339
if False:  # Never activate re-engagement mode
```

## Performance Impact

**Computational cost:** Near-zero
- Simple loop through recent_exchanges (typically 5-15 messages)
- One integer counter
- One string comparison for male names

**API cost:** Same as normal
- No additional AI calls
- Just adds instructions to existing prompt

## Summary

✅ **Activates after 6+ unanswered bot messages**
✅ **Switches from business to human/humor mode**
✅ **Uses self-aware humor, pattern interrupts, dad jokes (male names), or relatable moments**
✅ **Gives leads permission to opt out**
✅ **Automatically resets when lead responds**
✅ **No manual intervention required**

The bot now recognizes when it's being ignored and adapts its strategy to re-engage with humanity and humor instead of persistent sales pressure.
