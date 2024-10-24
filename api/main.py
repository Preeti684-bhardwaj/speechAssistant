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
import time

# Enhanced logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not found in environment variables")

# Use environment variable for base URL with fallback
BASE_URL = os.getenv('VERCEL_URL', 'your-default-url.vercel.app')
WEBSOCKET_URL = f"wss://{BASE_URL}"

SYSTEM_MESSAGE = (
    "आप एक मित्रवत लोन और म्यूचुअल फंड सलाहकार हैं जो हर किसी से एक करीबी दोस्त की तरह बात करते हैं। "
    "आपको हर उपयोगकर्ता की वित्तीय स्थिति और सपनों को समझना है। "
    "मानवीय स्पर्श के साथ सरल भाषा में बात करें जैसे आप उनके पुराने दोस्त हैं।"
    "हर जवाब के अंत में एक प्रासंगिक सवाल जरूर पूछें जो आगे की बातचीत को बढ़ावा दे।"
    "जवाब 30 शब्दों से ज्यादा न हों।"
    "यदि कोई प्रश्न लोन या म्यूचुअल फंड से संबंधित नहीं है, तो विनम्रता से कहें - 'माफ़ कीजिए, यह मेरी विशेषज्ञता का क्षेत्र नहीं है।'"
)

app = FastAPI()

# Connection tracking
active_connections = set()

async def keep_connection_alive(websocket: WebSocket):
    """Keep the WebSocket connection alive with periodic pings"""
    while True:
        try:
            await websocket.send_text(json.dumps({"type": "ping"}))
            await asyncio.sleep(20)  # Send ping every 20 seconds
        except Exception as e:
            logger.error(f"Keep-alive error: {str(e)}")
            break

async def send_session_update(openai_ws):
    """Send session update to OpenAI WebSocket with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": "alloy",
                    "instructions": SYSTEM_MESSAGE,
                    "modalities": ["text", "audio"],
                    "temperature": 0.8,
                }
            }
            await openai_ws.send(json.dumps(session_update))
            logger.info("Session update sent successfully")
            return
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to send session update: {str(e)}")
            if attempt == max_retries - 1:
                raise


@app.get("/api/health")
async def health_check():
    """Enhanced health check endpoint"""
    return {
        "status": "healthy",
        "openai_key_configured": bool(OPENAI_API_KEY),
        "base_url": BASE_URL,
        "active_connections": len(active_connections),
        "timestamp": time.time()
    }

@app.api_route("/api/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call with enhanced error handling"""
    try:
        response = VoiceResponse()
        
        # Longer initial pause for connection stability
        response.pause(length=2)
        response.say("WELCOME! to HI-VOKO, start speaking ..HELLO")
        
        # Use explicit websocket URL
        stream_url = f"{WEBSOCKET_URL}/api/media-stream"
        logger.info(f"Setting up stream connection with URL: {stream_url}")
        
        connect = Connect()
        connect.stream(
            url=stream_url,
            track="both_tracks",
            maxDuration=600,  # 10 minutes max duration
            parameter={
                "connectTimeout": 10000,  # 10 second connection timeout
                "reconnectAttempts": 3    # Number of reconnection attempts
            }
        )
        response.append(connect)
        
        return HTMLResponse(content=str(response), media_type="application/xml")
    except Exception as e:
        logger.error(f"Error in handle_incoming_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/api/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections with improved error handling and connection management"""
    connection_id = id(websocket)
    logger.info(f"New WebSocket connection attempt: {connection_id}")
    
    try:
        await websocket.accept()
        active_connections.add(connection_id)
        logger.info(f"WebSocket connection accepted: {connection_id}")
        
        # Start keep-alive task
        keep_alive_task = asyncio.create_task(keep_connection_alive(websocket))
        
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            },
            ping_interval=20,
            ping_timeout=60,
            close_timeout=10,
            max_size=2**23  # 8MB max message size
        ) as openai_ws:
            logger.info(f"Connected to OpenAI WebSocket: {connection_id}")
            await send_session_update(openai_ws)
            
            tasks = []
            stream_sid = None
            
            async def handle_twilio_message(message_data):
                """Process individual Twilio messages"""
                nonlocal stream_sid
                if message_data['event'] == 'start':
                    stream_sid = message_data['start']['streamSid']
                    logger.info(f"Stream started: {stream_sid}")
                elif message_data['event'] == 'media' and openai_ws.open:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": message_data['media']['payload']
                    }))

            async def receive_from_twilio():
                """Receive data from Twilio with improved error handling"""
                try:
                    async for message in websocket.iter_text():
                        await handle_twilio_message(json.loads(message))
                except WebSocketDisconnect:
                    logger.info(f"Twilio connection closed normally: {connection_id}")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                    raise

            async def send_to_twilio():
                """Send data to Twilio with improved error handling"""
                try:
                    async for message in openai_ws:
                        response = json.loads(message)
                        if response['type'] == 'response.audio.delta' and response.get('delta'):
                            audio_payload = base64.b64encode(
                                base64.b64decode(response['delta'])
                            ).decode('utf-8')
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload}
                            })
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {str(e)}")
                    raise

            tasks = [
                asyncio.create_task(receive_from_twilio()),
                asyncio.create_task(send_to_twilio())
            ]
            
            try:
                await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"Error in media stream tasks: {str(e)}")
                raise
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                
    except Exception as e:
        logger.error(f"Fatal error in WebSocket handler: {str(e)}")
    finally:
        if connection_id in active_connections:
            active_connections.remove(connection_id)
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"WebSocket connection closed: {connection_id}")