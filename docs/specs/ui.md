# UI specs

## Main flows

WIP. Follow the current implementation.

## Failed turn

When anything in the processing of a message fails in the backend, the user should receive an error response that will be displayed, bellow their last message, in the place where the normal agent would be, but with a different style, still indicating there was a failure.


Style should be just enough to clearly tell it was not a normal response, it should not be a flashing vibrant red or something like that. Use discretion to follow the general palette of the UI.

That same error notice will include a retry icon button.

After such failure, the user should be able to proceed with two normal flows, and one special case "retry":
1. Edit their message.
2. Reply with a new message.
3. Retry with the Refresh button.


The same conditions apply whether the failure occurs in the first message of a session or in the middle of a session.


In case (1) it is the same as any edit, the user change their mind, we should discard anything below that message, that is the general case of message editing.


In cases (2) and (3) we want the LLM to have hints of what failed in the past turn to either silently retry, try something different or just tell the user what is going wrong.

Note that the "Refresh" button in a failed response will behave different to the same button on a normal response. The "Refresh" button on a normal response is intended to omit that last response from the LLM view and let it generate a new one. The "Refresh" button is handled at the backend as "regenerate". When used from a failed response, the button keeps context of being a failure.


### Edit their message

In that case the LLM will not see the previous version user message that ended in error.


**Example**


User Message 1: What is the capital of France?
App Error: There was a problem processing your request, please retry or write a new message.
User edits Message 1: What is the capital of Spain?


The LLM should see only:

User Message 1: What is the capital of Spain?


### Reply with a new message

In that case the LLM will see the first message, the tool calling on the previous turn (best-effort), the error message we got to persist and display, and the new message from the user.



**Example**


User Message 1: What is the capital of France?
App Error: There was a problem processing your request, please retry or write a new message.
User Message 2: Was it my fault?


The LLM should see:

User Message 1: What is the capital of France?
Some tool calling if any from the failed turn
App Error:  There was a problem processing your request, please retry or write a new message.
User Message 2:  Was it my fault?


### Retry

The user press the Refresh button  in the error notice.


When the user press the Refresh button the error notice will disappear from the UI and the behavior should look as if there was a fresh message.


The LLM will see the first message as it was, the error, and a new message indicating the retry.


User can retry a failed response as many times as they want.


**Example**


User Message 1: What is the capital of France?
App Error: There was a problem processing your request, please retry or write a new message.
User press the Refresh button.


The LLM should see:

User Message 1: What is the capital of France?
Some tool calling if any from the failed turn
App Error:  There was a problem processing your request, please retry or write a new message.
Some app message: User pressed the Refresh button.


### Resume

The last answer, when it failed, also has a Resume button, beside Refresh.

Resume continues the answer's turn from the tool rounds the agent's engine saved. The LLM is
told nothing about the failure. Tool calls already saved do not run again, and a call that was
cut short runs again. When the engine saved nothing, the question runs again from the start.

Resume is for a long turn that failed or was interrupted, such as by a crash or a deploy.
Nothing resumes on its own. The conversation waits until the user, or another system through
the API, picks Refresh or Resume.
