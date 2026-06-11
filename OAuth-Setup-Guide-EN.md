# 🗓️ Google Calendar OAuth Setup — Step-by-Step Guide

> Goal: Let your "AI Calendar Task Planner" actually write tasks to Google Calendar.
> One-time setup, about 5-10 minutes.

---

## Step 1: Open Google Cloud Console

Open this link in your browser:

👉 **https://console.cloud.google.com/projectselector2/home/dashboard**

You'll see a page with "**Select a project**" or "**Create Project**" at the top.

### If this is your first time using Google Cloud:

1. Accept the Terms of Service (check the box → click "Agree and Continue")
2. The page will show "Select a project"

---

## Step 2: Create a New Project

1. Click the **"CREATE PROJECT"** button (blue, usually top-left or center)

2. In the pop-up window:
   - **Project name**: anything you want, e.g. `my-calendar-planner`
   - **Location**: leave as default (No organization)
3. Click **"CREATE"**

4. Wait a few seconds. A notification "Project created" will appear in the top-right corner. 
   Click **"SELECT PROJECT"** in that notification, or wait for the page to auto-redirect.

   > The project name should now appear at the top of the page. ✅

---

## Step 3: Enable the Google Calendar API

1. Open this link (in a new tab):

   👉 **https://console.cloud.google.com/apis/library/calendar-json.googleapis.com**

2. You'll see a page titled **"Google Calendar API"** with a blue button in the center.

3. Click the blue **"ENABLE"** button.

   ```
   ┌──────────────────────────┐
   │   Google Calendar API     │
   │                           │
   │   Google                   │
   │                           │
   │   [     E N A B L E     ] │  ← Click this blue button
   │                           │
   └──────────────────────────┘
   ```

4. Wait for the page to refresh and show "API Enabled". **You can close this tab now.**

---

## Step 4: Create OAuth Client ID (the most important step)

1. Open this link:

   👉 **https://console.cloud.google.com/apis/credentials**

   > Make sure the project name at the top is the one you just created.

2. Click the **"+ CREATE CREDENTIALS"** button at the top, then select **"OAuth client ID"**.

3. If you see "To create an OAuth client ID, you must first configure a consent screen" — click **"CONFIGURE CONSENT SCREEN"**:

   ### 4a. Configure Consent Screen

   1. Choose **"External"** (not "Internal"), then click **"CREATE"**
   
   2. Fill in these fields (**only the ones marked with * are required**):
      - **App name**: `My Calendar Planner`
      - **User support email**: select your Gmail
      - **Developer contact information**: enter your Gmail
   
   3. Skip ALL other fields — scroll straight to the bottom, click **"SAVE AND CONTINUE"**
   
   4. On the "Scopes" page: **don't change anything**, just click **"SAVE AND CONTINUE"**
   
   5. On the "Test users" page:
      - Click **"+ ADD USERS"**
      - Type your Gmail address
      - Click "ADD"
   
   6. Click **"SAVE AND CONTINUE"**
   
   7. Finally click **"BACK TO DASHBOARD"**

   > Now go back to the credentials page. If it didn't auto-redirect, open:
   > 👉 https://console.cloud.google.com/apis/credentials

4. Again click **"+ CREATE CREDENTIALS" → "OAuth client ID"**

5. In the "Application type" dropdown, select **"Desktop app"**

   ```
   Application type:  [Desktop app      ▾]
   Name:              [Desktop client 1   ]
   ```

6. Click **"CREATE"**

7. A pop-up appears showing your client ID. **Click "DOWNLOAD JSON"** in the bottom-right.

   ```
   ┌───────────────────────────────┐
   │  OAuth client created          │
   │                                │
   │  Your Client ID: xxxxx.apps... │
   │  Your Client Secret: xxxxxxxx  │
   │                                │
   │       [DOWNLOAD JSON]          │  ← Click this
   └───────────────────────────────┘
   ```

8. The file will download to your **Downloads** folder with a name like:
   ```
   client_secret_XXXXX-XXXXX.apps.googleusercontent.com.json
   ```

---

## Step 5: Tell Me the File Path

Send me the **full path** of the downloaded JSON file. For example:

```
My file is at: /Users/wangshiyu/Downloads/client_secret_xxx.json
```

> 💡 On Mac: Right-click the file in Finder → hold the **Option (⌥)** key → "Copy as Pathname", then paste it to me.

---

## Step 6: Authorization (I'll handle it)

Once you send me the file path, I will:
1. Run the authorization command
2. Give you a Google authorization link
3. You open that link in your browser and click "Allow"
4. Copy the ENTIRE URL from your browser's address bar and paste it to me
5. I'll complete the setup

**After this, your calendar planner can actually write to Google Calendar! 🎉**

---

## 📋 Quick Checklist

| Step | What to do | Done |
|------|-----------|------|
| 1 | Open Google Cloud Console | ☐ |
| 2 | Click "CREATE PROJECT" → name it | ☐ |
| 3 | Enable Calendar API → click "ENABLE" | ☐ |
| 4a | Configure Consent Screen (External) | ☐ |
| 4b | Add Test Users (your Gmail) | ☐ |
| 4c | Create OAuth Client ID (Desktop app) | ☐ |
| 4d | Click "DOWNLOAD JSON" | ☐ |
| 5 | Send me the JSON file path | ☐ |
| 6 | I'll do the rest | ☐ |

---

> If you get stuck at any step, tell me what page you're on and what you see. I'll help you through it.
