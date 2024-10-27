import websocket
import uuid
import json
import urllib.request
import urllib.parse
import requests
import sys
import logging
import os
import time
from Main.database import add_to_history
from Main.utils import generate_random_seed, load_json, save_json
import re
from config import server_address
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

client_id = str(uuid.uuid4())

def open_workflow(workflow_filename):
    """Opens and loads workflow file from DataSets directory"""
    try:
        workflow_path = f"Main/DataSets/{workflow_filename}"
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)
        logger.debug(f"Successfully loaded workflow from {workflow_path}")
        return workflow
    except FileNotFoundError:
        logger.error(f"Workflow file not found: {workflow_path}")
        raise FileNotFoundError(f"Workflow file not found: {workflow_path}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in workflow file: {e}")
        raise ValueError(f"Invalid JSON in workflow file: {workflow_filename}")

def update_workflow(workflow, prompt, resolution, loras, upscale_factor, seed):
    if not isinstance(workflow, dict):
        raise ValueError("Invalid workflow format")

    if '69' in workflow:
        workflow['69']['inputs']['prompt'] = prompt
    else:
        logger.warning("Node 69 not found in workflow")

    if '258' in workflow:
        workflow['258']['inputs']['ratio_selected'] = resolution
    else:
        logger.warning("Node 258 not found in workflow")

    if '271' in workflow:
        lora_loader = workflow['271']['inputs']
        lora_config = load_json('lora.json')
        lora_info = {lora['file']: lora for lora in lora_config['available_loras']}

        for key in list(lora_loader.keys()):
            if key.startswith('lora_'):
                del lora_loader[key]

        for i, lora in enumerate(loras, start=1):
            lora_key = f'lora_{i}'
            if lora in lora_info:
                lora_loader[lora_key] = {
                    'on': True,
                    'lora': lora,
                    'strength': lora_info[lora]['weight']
                }
            else:
                logger.warning(f"LoRA {lora} not found in lora.json")

    if '264' in workflow:
        workflow['264']['inputs']['scale_by'] = upscale_factor
    else:
        logger.warning("Node 264 not found in workflow")

    if '198:2' in workflow:
        workflow['198:2']['inputs']['noise_seed'] = seed
        logger.debug(f"Updated seed in workflow. New seed: {seed}")
    else:
        logger.warning("Node 198:2 not found in workflow")

    return workflow

def queue_prompt(prompt):
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    logger.debug(f"Sending workflow to ComfyUI: {data.decode('utf-8')}")
    req = urllib.request.Request(f"http://{server_address}:8188/prompt", data=data, method="POST")
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response_data = response.read().decode('utf-8')
            logger.debug(f"Response from ComfyUI: {response_data}")
            return json.loads(response_data)
    except Exception as e:
        logger.error(f"Error in queue_prompt: {str(e)}")
        raise

def get_image(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    url = f"http://{server_address}:8188/view?{url_values}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read(), filename
    except Exception as e:
        logger.error(f"Error in get_image: {str(e)}")
        raise

def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read())
    except Exception as e:
        logger.error(f"Error in get_history: {str(e)}")
        raise

def clear_cache(ws):
    clear_message = json.dumps({"type": "clear_cache"})
    ws.send(clear_message)
    logger.debug("Sent clear_cache message to ComfyUI")

def get_images(ws, workflow, progress_callback):
    try:
        prompt_response = queue_prompt(workflow)
        if 'prompt_id' not in prompt_response:
            logger.error(f"Unexpected response from queue_prompt: {prompt_response}")
            raise ValueError("No prompt_id in response from queue_prompt")
        prompt_id = prompt_response['prompt_id']
        
        output_images = {}
        start_time = time.time()
        executing = False
        max_steps = 1
        last_milestone = 0
        
        # Track loading states
        loading_states = {
            "model": False,
            "clip": False,
            "vae": False,
            "loras": []
        }
        
        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                
                if message['type'] == 'execution_start':
                    progress_callback({"status": "starting", "message": "Starting execution..."})
                
                elif message['type'] == 'executing':
                    data = message['data']
                    
                    # Track model loading
                    if "UNETLoader" in str(data):
                        loading_states["model"] = True
                        progress_callback({"status": "loading", "message": "Loading main model..."})
                    
                    elif "CLIPLoader" in str(data):
                        loading_states["clip"] = True
                        progress_callback({"status": "loading", "message": "Loading CLIP model..."})
                    
                    elif "VAELoader" in str(data):
                        loading_states["vae"] = True
                        progress_callback({"status": "loading", "message": "Loading VAE..."})
                    
                    elif "Power Lora Loader" in str(data):
                        current_lora = data.get('node_info', {}).get('title', 'LoRA')
                        loading_states["loras"].append(current_lora)
                        progress_callback({
                            "status": "loading", 
                            "message": f"Loading LoRA: {current_lora}"
                        })
                    
                    # Check if execution is complete
                    if data['node'] is None and data['prompt_id'] == prompt_id:
                        break
                
                elif message['type'] == 'progress':
                    data = message['data']
                    current_step = data['value']
                    max_steps = data['max']
                    progress = int((current_step / max_steps) * 100)
                    
                    current_milestone = (progress // 10) * 10
                    if current_milestone > last_milestone and current_milestone < 100:
                        progress_callback({
                            "status": "generating",
                            "message": f"Generating image... {progress}%",
                            "progress": progress
                        })
                        last_milestone = current_milestone
                    
                    progress_time_used = round((time.time() - start_time) / 60, 2)
                    logger.info(f"Progress: {current_step}/{max_steps} ({progress}%) - Time: {progress_time_used} min")
                
                elif message['type'] == 'execution_cached':
                    progress_callback({
                        "status": "cached",
                        "message": "Using cached result..."
                    })
        
        progress_callback({
            "status": "complete",
            "message": "Generation complete!",
            "progress": 100
        })
        
        # Get the final images
        history = get_history(prompt_id)[prompt_id]
        for node_id, node_output in history['outputs'].items():
            if 'images' in node_output:
                images_output = []
                for image in node_output['images']:
                    image_data, filename = get_image(image['filename'], image['subfolder'], image['type'])
                    images_output.append((image_data, filename))
                output_images[node_id] = images_output
        
        return output_images
        
    except Exception as e:
        logger.error(f"Error in get_images: {str(e)}")
        progress_callback({
            "status": "error",
            "message": f"Error during generation: {str(e)}"
        })
        raise

def calculate_upscaled_resolution(resolution, upscale_factor):
    try:
        
        ratios_config = load_json('ratios.json')
        
    
        if resolution not in ratios_config['ratios']:
            raise ValueError(f"Resolution {resolution} not found in ratios configuration")
            
        base_res = ratios_config['ratios'][resolution]
        width = base_res['width']
        height = base_res['height']

        
        final_width = width * upscale_factor
        final_height = height * upscale_factor
        
        return f"{final_width}x{final_height}"
    except Exception as e:
        logger.error(f"Error calculating upscaled resolution: {str(e)}")
        raise ValueError(f"Unable to calculate upscaled resolution: {str(e)}")

def send_progress_update(request_id, progress_data):
    try:
        data = {
            'request_id': request_id,
            'progress_data': progress_data
        }
        response = requests.post(f"http://{server_address}:8080/update_progress", json=data, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Progress update failed with status {response.status_code}: {response.text}")
        else:
            logger.debug(f"Progress update sent: {progress_data}")
    except Exception as e:
        logger.error(f"Error sending progress update: {str(e)}")

# And update the get_images function's progress callback usage:
progress_callback = lambda progress: send_progress_update(request_id, {
    'status': 'generating' if isinstance(progress, int) else progress.get('status', 'processing'),
    'message': progress.get('message', f'Generating image... {progress}% complete') if isinstance(progress, dict) else f'Generating image... {progress}% complete',
    'progress': progress if isinstance(progress, int) else progress.get('progress', 0)
})

if __name__ == "__main__":
    try:
        if len(sys.argv) < 11:
            raise ValueError(f"Expected at least 10 arguments, but got {len(sys.argv) - 1}")

        request_id = sys.argv[1]
        user_id = sys.argv[2]
        channel_id = sys.argv[3]
        interaction_id = sys.argv[4]
        original_message_id = sys.argv[5]
        full_prompt = sys.argv[6]
        resolution = sys.argv[7]
        loras = json.loads(sys.argv[8])
        upscale_factor = int(sys.argv[9])
        workflow_filename = sys.argv[10]
        seed = sys.argv[11] if len(sys.argv) > 11 else None

        logger.debug(f"Received arguments: request_id={request_id}, user_id={user_id}, "
                    f"channel_id={channel_id}, interaction_id={interaction_id}, "
                    f"original_message_id={original_message_id}, full_prompt='{full_prompt}', "
                    f"resolution='{resolution}', loras={loras}, upscale_factor={upscale_factor}, "
                    f"workflow_filename='{workflow_filename}', seed={seed}")

        # Send initial status
        send_progress_update(request_id, {
            'status': 'starting',
            'message': 'Starting generation process...',
            'progress': 0
        })

        # Load workflow
        try:
            send_progress_update(request_id, {
                'status': 'loading',
                'message': 'Loading workflow...',
                'progress': 5
            })
            workflow = open_workflow(workflow_filename)
            if not isinstance(workflow, dict):
                raise ValueError(f"Invalid workflow format in {workflow_filename}")
        except Exception as e:
            logger.error(f"Error loading workflow: {str(e)}")
            raise

        # Process seed
        try:
            send_progress_update(request_id, {
                'status': 'loading',
                'message': 'Initializing parameters...',
                'progress': 10
            })
            seed = int(seed) if seed != "None" else generate_random_seed()
            logger.debug(f"Using seed: {seed}")
        except ValueError as e:
            logger.error(f"Invalid seed value: {str(e)}")
            raise

        # Update workflow
        try:
            workflow = update_workflow(workflow, full_prompt, resolution, loras, upscale_factor, seed)
            logger.debug(f"Updated workflow content: {json.dumps(workflow, indent=2)}")
        except Exception as e:
            logger.error(f"Error updating workflow: {str(e)}")
            raise

        # Calculate upscaled resolution
        try:
            upscaled_resolution = calculate_upscaled_resolution(resolution, upscale_factor)
        except ValueError as e:
            logger.error(f"Error calculating upscaled resolution: {str(e)}")
            upscaled_resolution = "Unknown"

        # Connect to WebSocket
        try:
            send_progress_update(request_id, {
                'status': 'loading',
                'message': 'Connecting to ComfyUI...',
                'progress': 15
            })
            logger.debug(f"Connecting to WebSocket at ws://{server_address}:8188/ws?clientId={client_id}")
            ws = websocket.create_connection(f"ws://{server_address}:8188/ws?clientId={client_id}", timeout=30)
        except Exception as e:
            logger.error(f"Error connecting to WebSocket: {str(e)}")
            raise

        try:
            # Clear cache and prepare for generation
            logger.debug("WebSocket connected. Clearing cache...")
            clear_cache(ws)
            
            send_progress_update(request_id, {
                'status': 'loading',
                'message': 'Loading models and preparing generation...',
                'progress': 20
            })

            # Generate images
            images = get_images(ws, workflow, lambda p: send_progress_update(request_id, p))
            logger.debug(f"Received images: {len(images)} nodes with output")

            # Process output images
            final_image = None
            for node_id, image_data_list in reversed(images.items()):
                logger.debug(f"Checking node {node_id} for final image")
                for image_data, filename in reversed(image_data_list):
                    if not filename.startswith('ComfyUI_temp'):
                        final_image = (image_data, filename)
                        logger.debug(f"Found final image: {filename}")
                        break
                if final_image:
                    break

            if final_image:
                image_data, filename = final_image
                files = {'image_data': (filename, image_data)}
                data = {
                    'request_id': request_id,
                    'user_id': user_id,
                    'channel_id': channel_id,
                    'interaction_id': interaction_id,
                    'original_message_id': original_message_id,
                    'prompt': full_prompt,
                    'resolution': resolution,
                    'upscaled_resolution': upscaled_resolution,
                    'loras': json.dumps(loras),
                    'upscale_factor': upscale_factor,
                    'seed': seed
                }
                
                send_progress_update(request_id, {
                    'status': 'finalizing',
                    'message': 'Sending generated image...',
                    'progress': 95
                })

                logger.debug(f"Sending request to web server with data: {data}")
                response = requests.post(f"http://{server_address}:8080/send_image", 
                                      data=data, 
                                      files=files, 
                                      timeout=30)
                logger.debug(f"Response from web server: {response.text}")
                
                send_progress_update(request_id, {
                    'status': 'complete',
                    'message': 'Generation complete!',
                    'progress': 100
                })
                
                print(response.text)

                # Add to history
                add_to_history(user_id, full_prompt, workflow, filename, resolution, loras, upscale_factor)
            else:
                logger.error("No final image found to send.")
                send_progress_update(request_id, {
                    'status': 'error',
                    'message': 'No final image generated',
                    'progress': 0
                })
                print("Error: No final image found to send.")

        except Exception as e:
            logger.error(f"Error during image generation/processing: {str(e)}", exc_info=True)
            send_progress_update(request_id, {
                'status': 'error',
                'message': f'Error during generation: {str(e)}',
                'progress': 0
            })
            raise
        finally:
            # Clean up
            try:
                ws.close()
                logger.debug("WebSocket connection closed")
            except Exception as e:
                logger.error(f"Error closing WebSocket: {str(e)}")

            try:
                os.remove(f"Main/DataSets/{workflow_filename}")
                logger.debug(f"Deleted workflow file: {workflow_filename}")
            except Exception as e:
                logger.error(f"Error removing workflow file: {str(e)}")

    except ValueError as ve:
        logger.error(f"Argument error: {str(ve)}")
        send_progress_update(request_id, {
            'status': 'error',
            'message': f'Configuration error: {str(ve)}',
            'progress': 0
        })
        print(f"Error: {str(ve)}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in comfygen.py: {str(e)}", exc_info=True)
        send_progress_update(request_id, {
            'status': 'error',
            'message': f'Unexpected error: {str(e)}',
            'progress': 0
        })
        print(f"Error: {str(e)}")
    finally:
        if 'ws' in locals():
            ws.close()
