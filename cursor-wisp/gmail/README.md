# Gmail for the Weave pet

Unpacked Chrome extension. It reads the open compose box and does not send mail.

## Load it

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose this folder (`cursor-wisp/gmail`).

The pet app needs to be running. This extension does not call the model itself.

## @bubble

The pet stays quiet until the message body contains `@bubble`. A pause by itself is not enough.

- `@bubble email the sales team this quarter's profit margin` asks for a full draft.
- `our margin this quarter is @bubble` asks for one sentence.

When the text has sat still for a moment, the pet looks it up and opens its card.

**Accept** writes that text into the compose box in place of `@bubble`. It does not send the email. You still press Send in Gmail.

**Ignore** closes the card and leaves the draft as it is.
