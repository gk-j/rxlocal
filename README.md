# RefillQ

## AI-Powered Prescription Refill & Patient Engagement

**RefillQ helps pharmacies turn upcoming prescription refills into timely patient conversations and qualified refill opportunities.**

Pharmacies already have valuable information about when patients purchased their medications, how frequently they take them, and approximately when their medication supply may run out.

The challenge is acting on that information at the right time.

RefillQ uses prescription and purchase data to identify patients approaching their next refill, proactively contacts them through text, understands their needs through AI-powered conversations, and provides pharmacy teams with qualified opportunities for follow-up.

**Right Patient. Right Medication. Right Time.**

---

## The Business Problem

A prescription purchase is often not a one-time transaction.

Patients managing ongoing conditions may need to purchase the same medication every few weeks or months. However, pharmacies cannot manually track and contact every patient at exactly the right time.

As a result:

- Patients may forget to refill their medications.
- Pharmacies may miss repeat-purchase opportunities.
- Staff spend time manually identifying patients to contact.
- Refill outreach may happen too early or too late.
- High-value opportunities can become buried within thousands of patient records.

The pharmacy already has the data.

**RefillQ turns that data into actionable refill opportunities.**

---

## How RefillQ Works

### 1. Identify the Refill Window

RefillQ uses information such as:

- Medication purchased
- Purchase date
- Prescribed consumption frequency
- Expected medication supply
- Treatment duration

to estimate when a patient may be approaching their next refill.

This creates a prioritized queue of patients who are likely approaching their next purchase.

### 2. Proactive Text Outreach

When a patient approaches their expected refill window, RefillQ initiates a text-based conversation.

Instead of waiting for the patient to remember their refill, **RefillQ reaches them at the right time.**

### 3. AI-Powered Conversation

The AI assistant conducts a natural text conversation with the patient.

It can understand whether the patient:

- Is still taking the medication
- Requires a refill
- Is feeling better
- Feels the medication is working
- Is experiencing side effects
- Has already purchased the medication
- Wants pharmacy assistance
- Wants to speak with a healthcare professional

The conversation adapts based on the patient's responses rather than following a rigid questionnaire.

### 4. Convert Conversations Into Leads

RefillQ analyzes the conversation in the background and converts patient responses into actionable information.

For example:

**Needs medication → Refill opportunity**

**Still has medication → Follow up later**

**Already purchased → No immediate refill opportunity**

**Reports a health concern → Human review**

This helps the pharmacy focus on patients who actually require attention.

### 5. Pharmacy Intelligence Dashboard

The results are presented to pharmacy staff through the Intelligence Dashboard.

The pharmacy team can review:

- Patient
- Medication
- Condition
- Expected refill timing
- Text outreach status
- Conversation insights
- Refill interest
- Follow-up priority
- Human decision
- Steps taken

Instead of manually searching through patient records, pharmacy staff receive a **prioritized refill pipeline.**

---

## From Prescription Data to Refill Opportunity

The core RefillQ workflow is:

**Prescription & Purchase Data  
→ Refill Window Identification  
→ Patient Prioritization  
→ Automated Text Outreach  
→ AI Conversation  
→ Lead Qualification  
→ Intelligence Dashboard  
→ Human Follow-Up  
→ Refill Opportunity**

---

## Business Value

### For Pharmacies

RefillQ helps pharmacies:

- Generate qualified refill leads
- Identify repeat-purchase opportunities
- Improve patient re-engagement
- Reduce manual follow-up work
- Prioritize patients most likely to require a refill
- Improve visibility into upcoming refill opportunities
- Support recurring prescription sales

### For Patients

Patients receive timely text reminders when they may be approaching the end of their medication supply.

They can simply respond through text and communicate whether they need a refill or additional pharmacy assistance.

---

## More Than a Reminder

A traditional system may simply send:

**"Your medication may be due for refill."**

RefillQ goes further.

It understands the patient's response, continues the conversation, identifies their refill intent, and provides relevant information back to the pharmacy.

RefillQ therefore acts as an **AI-powered refill engagement and lead qualification system**, rather than a simple notification tool.

---

## Human-in-the-Loop

RefillQ automates patient engagement and opportunity identification — not healthcare decisions.

The AI can:

- Initiate text conversations
- Collect patient responses
- Identify refill intent
- Analyze conversations
- Prioritize potential opportunities

The pharmacy team remains responsible for the final action.

**AI identifies and qualifies the opportunity. Humans make the decision.**

---

## Safety & Privacy

RefillQ operates within clear boundaries.

The AI does not:

- Diagnose medical conditions
- Prescribe medications
- Recommend changing medications
- Modify dosage
- Provide medical advice

Situations requiring medical judgment are escalated for human review.

Internal conversation analysis and pharmacy intelligence remain in the backend and are never exposed to the patient.

---

## Technology Stack

RefillQ is built around a locally deployable AI architecture.

**Local Large Language Model (LLM)** — Powers text conversations and conversation analysis while keeping AI processing locally controlled.

**Python** — Powers backend services, business logic, data processing, and AI workflows.

**Model Context Protocol (MCP)** — Provides controlled interaction between the AI and approved application data and tools.

**MongoDB** — Stores patient, medication, messaging, and operational information.

**Web Application** — Provides pharmacy teams with the operational workflow and Intelligence Dashboard.

**Telegram** — Serves as the text-messaging interface for patient conversations in the current prototype.

---

## What Makes RefillQ Different?

Most reminder systems answer:

**"Who should receive a refill reminder?"**

RefillQ answers more:

**Who is approaching a refill?  
→ Who should we contact?  
→ What did the patient say?  
→ Do they actually need medication?  
→ Is this a qualified refill opportunity?  
→ Who should the pharmacy prioritize?**

RefillQ connects **patient data, proactive text outreach, conversational AI, lead qualification, and human action** into one workflow.

---

## Built For

**Dell AI Hackathon — New York City**

RefillQ demonstrates how locally deployed AI can transform pharmacy prescription data into proactive text-based patient engagement and qualified refill opportunities — helping pharmacies improve patient retention, operational efficiency, and recurring prescription sales.
