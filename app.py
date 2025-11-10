import os
import base64
import io
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
import fitz # PyMuPDF
from PIL import Image # Pillow is often used for image handling, but not strictly needed for base64 encoding from bytes

# Uses the environment variable OPENAI_API_KEY
API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = "gpt-4o-mini"

if not API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable not set.")

app = FastAPI()
client = AsyncOpenAI(api_key=API_KEY)

# Store for associating uploaded file data with the user's next prompt
# In a real-world app, you'd use a database or cache with user/session IDs.
# For this example, we use a simple dict mapping session_id (which is just the first part of the message here)
uploaded_data_store = {}

# --- Utility Functions for File Processing ---

def pdf_to_text(file_bytes: bytes) -> str:
    """Extracts text from a PDF file."""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text[:15000] # Limit text to prevent excessively large prompts
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return "Could not extract text from the PDF file."

def image_to_base64(file_bytes: bytes, mime_type: str) -> str:
    """Converts image bytes to a Base64 string for OpenAI's vision model."""
    # The bytes are already the image data, we just encode them
    base64_image = base64.b64encode(file_bytes).decode('utf-8')
    return f"data:{mime_type};base64,{base64_image}"

# --- FastAPI Endpoints ---

@app.post("/uploadfile")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(..., description="Unique ID for this session/connection")
):
    """Handles file uploads via a standard HTTP POST request."""
    print(f"Received file upload for session_id: {session_id}")
    
    file_bytes = await file.read()
    mime_type = file.content_type
    processed_content = None
    
    if mime_type.startswith('image/'):
        # Process image file
        processed_content = image_to_base64(file_bytes, mime_type)
        content_type_description = "an image"
    elif mime_type == 'application/pdf':
        # Process PDF file
        extracted_text = pdf_to_text(file_bytes)
        # Note: For PDF, we send the extracted text directly, not a special object.
        processed_content = f"The user uploaded a PDF. The extracted text is as follows:\n---\n{extracted_text}\n---\n"
        content_type_description = "a PDF"
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type.")

    # Store the processed content in the global store
    # The client will use the session_id (which is actually just an identifier here) to retrieve it later.
    uploaded_data_store[session_id] = processed_content
    
    return JSONResponse(content={
        "message": f"Successfully uploaded and processed {content_type_description}.",
        "file_type": mime_type,
        "session_id": session_id
    })

# The client-side JavaScript connects to ws://localhost:8000/ws
html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>FastAPI WebSocket Chatbot</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f9; }
        #chat-box { max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 15px; }
        #messages { height: 300px; overflow-y: scroll; border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; border-radius: 4px; }
        .user-msg { text-align: right; color: #007bff; margin: 5px 0; }
        .bot-msg { text-align: left; color: #28a745; margin: 5px 0; white-space: pre-wrap; }
        .cursor::after {
            content: "|";
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0; }
        }
        #input-container { display: flex; flex-direction: column; }
        #file-upload-container { margin-bottom: 10px; display: flex; align-items: center; }
        #file-input { margin-right: 10px; }
        #chat-form { display: flex; }
        #message-input { flex-grow: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        #send-btn { width: 18%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; margin-left: 10px; }
        #send-btn:hover { background-color: #0056b3; }
        #upload-status { margin-top: 5px; font-size: 0.9em; color: #e9b300; }
    </style>
</head>
<body>
    <div id="chat-box">
        <h2>FastAPI WebSocket Bot (gpt-4o-mini)</h2>
        <div id="messages">
            <div class="bot-msg">🤖 Initializing connection...</div>
        </div>
        
        <div id="input-container">
        <div id="file-upload-container">
    <input type="file" id="file-input" accept="image/*,application/pdf" style="display: none;">
    <button onclick="document.getElementById('file-input').click()" id="choose-and-upload-btn">
        Choose & Upload File
    </button>
</div>
            <div id="upload-status"></div>
            <form id="chat-form" onsubmit="sendMessage(event)">
                <input type="text" id="message-input" placeholder="Type your message..." required>
                <button type="submit" id="send-btn" disabled>Send</button>
            </form>
        </div>
    </div>

    <script>
        const input = document.getElementById('message-input');
        const fileInput = document.getElementById('file-input');
        const uploadStatus = document.getElementById('upload-status');
        const messagesContainer = document.getElementById('messages');
        const sendButton = document.getElementById('send-btn');
        // A simple way to generate a session ID for a simple example
        const SESSION_ID = 'session-' + Date.now(); 
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(`${protocol}//${window.location.host}/ws?session_id=${SESSION_ID}`);


        let currentBotMessageElement = null;
        let uploadedFileData = null; // Stores info about the last uploaded file for the next prompt
fileInput.addEventListener('change', (event) => {
            if (fileInput.files.length > 0) {
                uploadFile();
            }
        });
        function appendMessage(sender, text, isStreaming = false) {
            const msgDiv = document.createElement('div');
            msgDiv.className = sender === 'user' ? 'user-msg' : 'bot-msg';
            const prefix = sender === 'user' ? '🧑' : '🤖';
            msgDiv.textContent = `${prefix} ${text}`;
            
            if (isStreaming) {
                currentBotMessageElement = msgDiv;
                msgDiv.classList.add('streaming-msg');
                msgDiv.innerHTML = `${prefix} <span class="text-content"></span><span class="cursor"></span>`;
            }

            messagesContainer.appendChild(msgDiv);
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
            return msgDiv.querySelector('.text-content') || msgDiv;
        }

        ws.onopen = function(event) {
            messagesContainer.innerHTML = '';
            appendMessage('bot', '✅ Connection established. I am gpt-4o-mini. Ask me anything!');
            sendButton.disabled = false;
        };

        ws.onmessage = function(event) {
            const chunk = event.data;
            
            if (!currentBotMessageElement) {
                const textSpan = appendMessage('bot', '', true);
                textSpan.textContent += chunk;
            } else {
                const textSpan = currentBotMessageElement.querySelector('.text-content');
                if (textSpan) {
                    textSpan.textContent += chunk;
                }
            }

            messagesContainer.scrollTop = messagesContainer.scrollHeight; 
        };
        
        ws.onclose = function(event) {
            if (currentBotMessageElement) {
                const cursor = currentBotMessageElement.querySelector('.cursor');
                if (cursor) cursor.remove();
            }
            appendMessage('bot', '🚫 Connection closed by the server or client.');
            sendButton.disabled = true;
        };

        ws.onerror = function(event) {
            appendMessage('bot', '🚨 An error occurred with the WebSocket.');
            sendButton.disabled = true;
        };
        
        async function uploadFile() {
            const file = fileInput.files[0];
            if (!file) {
                uploadStatus.textContent = "Please select a file first.";
                return;
            }

            uploadStatus.textContent = `Uploading ${file.name}...`;
            
            const formData = new FormData();
            formData.append('file', file);
            formData.append('session_id', SESSION_ID); // Send the session ID for the server to store the file data

            try {
                const response = await fetch('/uploadfile', {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }

                const data = await response.json();
                uploadedFileData = data; // Store the response for use in sendMessage
                uploadStatus.textContent = `✅ File ${file.name} uploaded. Type your prompt to analyze it.`;
                fileInput.value = ''; // Clear file input
            } catch (error) {
                console.error('Upload failed:', error);
                uploadStatus.textContent = `❌ File upload failed: ${error.message}`;
                uploadedFileData = null;
            }
        }

        function sendMessage(event) {
            event.preventDefault();
            let userMessage = input.value.trim();
            
            if (!userMessage && !uploadedFileData) return; // Must have text or uploaded file

            const messagePayload = {
                text: userMessage,
                file_info: uploadedFileData,
                session_id: SESSION_ID // Include session ID for the server to check for file data
            };

            // Display message to the user
            let displayMessage = userMessage;
            if (uploadedFileData) {
                const fileType = uploadedFileData.file_type.startsWith('image') ? 'Image' : 'PDF';
                displayMessage = `[${fileType} Attached] ${userMessage || 'Analyze file'}`;
                // Clear the file data flag after creating the payload
                uploadedFileData = null; 
            }
            appendMessage('user', displayMessage);
            
            // Clear any previous streaming state
            currentBotMessageElement = null;

            // Send a JSON string over the WebSocket
            ws.send(JSON.stringify(messagePayload)); 
            input.value = '';
            uploadStatus.textContent = ''; // Clear file status after sending
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def serve_chatbot_ui() -> HTMLResponse:
    """Serves the main HTML page for the chatbot."""
    return HTMLResponse(html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Retrieve the session_id from the connection URL
    session_id = websocket.query_params.get("session_id")
    if not session_id:
        await websocket.close(code=1008, reason="Missing session_id in query parameters.")
        return

    await websocket.accept()
    
    chat_history = [{"role": "system", "content": "If the user provides a blood test image or PDF, summarize the results and provide general, non-medical advice. Don't respond to anything not blood test related. Keep it short. Be helpful."}]

    try:
        while True:
            # 1. Receive the user message (now expects a JSON string)
            raw_data = await websocket.receive_text()
            import json
            try:
                message_payload = json.loads(raw_data)
                user_message = message_payload.get("text", "")
                # Check for file content associated with this session ID
                file_content_data = uploaded_data_store.pop(session_id, None)
            except json.JSONDecodeError:
                # Fallback for plain text message if JSON parsing fails
                user_message = raw_data
                file_content_data = None


            # 2. Build the message content for OpenAI
            message_content = []
            
            # a) Handle image/PDF content if present
            if file_content_data:
                # Check if it's a Base64 image string (starts with 'data:image/')
                if isinstance(file_content_data, str) and file_content_data.startswith("data:image/"):
                    # For images, the content is an object list with text and image parts
                    image_url = file_content_data
                    # Add a default text part to provide context for the image
                    message_content.append({"type": "text", "text": user_message or "Analyze this image and respond to my prompt."})
                    message_content.append({"type": "image_url", "image_url": {"url": image_url}})
                    
                else: 
                    # This handles PDF text extraction, which is a regular string
                    message_content.append({"type": "text", "text": file_content_data + "\n\nUser prompt: " + user_message})

            elif user_message:
                # b) Handle text-only message
                message_content.append({"type": "text", "text": user_message})

            if not message_content:
                continue # Skip if no message and no file content

            # 3. Add user message to history in OpenAI format
            chat_history.append({"role": "user", "content": message_content})

            # 4. Request streaming response from OpenAI
            stream = await client.chat.completions.create(
                # gpt-4o-mini is vision-capable
                model=MODEL_NAME, 
                messages=chat_history,
                stream=True,
            )

            # Initialize bot response storage for history update
            full_bot_response = ""
            
            # 5. Process and stream chunks
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                
                if content:
                    await websocket.send_text(content)
                    full_bot_response += content

            # 6. Add the *complete* bot response to the chat history for context
            if full_bot_response:
                chat_history.append({"role": "assistant", "content": full_bot_response})

    except WebSocketDisconnect:
        print(f"Client {session_id} disconnected.")
    except Exception as e:
        print(f"An error occurred for {session_id}: {e}")
        try:
            await websocket.send_text(f"ERROR: {e}")
            await websocket.close()
        except:
            pass # Ignore errors on close
