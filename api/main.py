# api/index.py
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
import os
import json
import base64
import asyncio
import websockets
from dotenv import load_dotenv
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(_name_)

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not found in environment variables")

SYSTEM_MESSAGE = (
    "आप एक मित्रवत लोन और म्यूचुअल फंड सलाहकार हैं जो हर किसी से एक करीबी दोस्त की तरह बात करते हैं। "
    "आपको हर उपयोगकर्ता की वित्तीय स्थिति और सपनों को समझना है। "
    "मानवीय स्पर्श के साथ सरल भाषा में बात करें जैसे आप उनके पुराने दोस्त हैं।"
    "हर जवाब के अंत में एक प्रासंगिक सवाल जरूर पूछें जो आगे की बातचीत को बढ़ावा दे।"
    "जवाब 30 शब्दों से ज्यादा न हों।"
    "यदि कोई प्रश्न लोन या म्यूचुअल फंड से संबंधित नहीं है, तो विनम्रता से कहें - 'माफ़ कीजिए, यह मेरी विशेषज्ञता का क्षेत्र नहीं है।'"
)
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]

app = FastAPI()

async def send_session_update(openai_ws):
    """Send session update to OpenAI WebSocket."""
    try:
        session_update = {
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": SYSTEM_MESSAGE,
                "modalities": ["text", "audio"],
                "temperature": 0.8,
            }
        }
        logger.info('Sending session update to OpenAI')
        await openai_ws.send(json.dumps(session_update))
        logger.info('Session update sent successfully')
    except Exception as e:
        logger.error(f"Error in send_session_update: {str(e)}")
        raise

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "openai_key_configured": bool(OPENAI_API_KEY),
    }

@app.get("/api", response_class=HTMLResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/api/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    try:
        response = VoiceResponse()
        response.pause(length=1)
        response.say("WELCOME! to HI-VOKO, start speaking ..HELLO")
        
        # Get the request base URL for WebSocket connection
        base_url = str(request.base_url)
        if base_url.startswith('http://'):
            base_url = base_url.replace('http://', 'https://', 1)
        
        logger.info(f"Setting up WebSocket connection at base URL: {base_url}")
        
        connect = Connect()
        stream_url = f"{base_url}api/media-stream"
        connect.stream(url=stream_url)
        response.append(connect)
        
        logger.info(f"TwiML response created with stream URL: {stream_url}")
        return HTMLResponse(content=str(response), media_type="application/xml")
    except Exception as e:
        logger.error(f"Error in handle_incoming_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/api/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    logger.info("New WebSocket connection attempt")
    await websocket.accept()
    logger.info("WebSocket connection accepted")

    try:
        openai_ws_url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'
        logger.info(f"Connecting to OpenAI WebSocket at {openai_ws_url}")
        
        async with websockets.connect(
            openai_ws_url,
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            },
            ping_interval=20,
            ping_timeout=20
        ) as openai_ws:
            logger.info("Connected to OpenAI WebSocket")
            await send_session_update(openai_ws)
            stream_sid = None

            async def receive_from_twilio():
                nonlocal stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data['event'] == 'media' and openai_ws.open:
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                            logger.debug("Audio data sent to OpenAI")
                        elif data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            logger.info(f"Stream started with SID: {stream_sid}")
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                finally:
                    if openai_ws.open:
                        await openai_ws.close()
                        logger.info("OpenAI WebSocket closed")

            async def send_to_twilio():
                nonlocal stream_sid
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        if response['type'] in LOG_EVENT_TYPES:
                            logger.info(f"OpenAI event: {response['type']}")
                            logger.debug(f"OpenAI response: {response}")
                        
                        if response['type'] == 'session.updated':
                            logger.info("Session updated successfully")
                        
                        if response['type'] == 'response.audio.delta' and response.get('delta'):
                            try:
                                audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": audio_payload
                                    }
                                }
                                await websocket.send_json(audio_delta)
                                logger.debug("Audio response sent to Twilio")
                            except Exception as e:
                                logger.error(f"Error processing audio data: {str(e)}")
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {str(e)}")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            
    except websockets.exceptions.ConnectionClosed as e:
        logger.error(f"WebSocket connection closed unexpectedly: {str(e)}")
    except Exception as e:
        logger.error(f"Error in handle_media_stream: {str(e)}")
    finally:
        logger.info("WebSocket connection handling completed")