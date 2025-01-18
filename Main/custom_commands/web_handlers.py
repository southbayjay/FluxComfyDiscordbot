import discord
from aiohttp import web
import logging
import json
import io
from Main.database import add_to_history
from Main.utils import load_json
from .models import RequestItem, ReduxRequestItem, ReduxPromptRequestItem
from typing import Dict, Any
import asyncio
from .message_constants import STATUS_MESSAGES
from .views import ImageControlView, ReduxImageView, PuLIDImageView

logger = logging.getLogger(__name__)

async def handle_generated_image(request):
    try:
        logger.debug("Received request to handle_generated_image")
        reader = await request.multipart()
        request_data = {
            'request_id': None,
            'user_id': None,
            'channel_id': None,
            'interaction_id': None,
            'original_message_id': None,
            'prompt': None,
            'resolution': None,
            'upscaled_resolution': None,
            'loras': None,
            'upscale_factor': None,
            'seed': None,
            'image_data': None
        }

        # Read multipart data
        async for part in reader:
            if part.name == 'image_data':
                request_data['image_data'] = await part.read(decode=False)
            elif part.name == 'loras':
                request_data['loras'] = json.loads(await part.text())
            elif part.name == 'upscale_factor':
                try:
                    request_data['upscale_factor'] = int(await part.text())
                except (ValueError, TypeError):
                    request_data['upscale_factor'] = 1
            else:
                request_data[part.name] = await part.text()

        # Validate required fields
        required_fields = [
            'request_id', 'user_id', 'channel_id', 'interaction_id',
            'original_message_id', 'prompt', 'resolution', 'image_data'
        ]
        
        missing_fields = [field for field in required_fields if not request_data[field]]
        if missing_fields:
            logger.warning(f"Missing required fields: {', '.join(missing_fields)}")
            return web.Response(text="Missing required data", status=400)

        # Check if request is still pending
        if request_data['request_id'] not in request.app['bot'].pending_requests:
            logger.warning(f"Received response for unknown request_id: {request_data['request_id']}")
            return web.Response(text="Unknown request", status=404)

        # Get request item to check type
        request_item = request.app['bot'].pending_requests[request_data['request_id']]

        try:
            # Fetch necessary Discord objects
            user = await request.app['bot'].fetch_user(int(request_data['user_id']))
            channel = await request.app['bot'].fetch_channel(int(request_data['channel_id']))
            guild = channel.guild
            member = await guild.fetch_member(int(request_data['user_id']))
            
            user_name = user.display_name if user else "Unknown User"
            user_color = member.color.value if member.color.value != 0 else 0x5DADEC

            # Create embed
            embed = discord.Embed(
                title=f"Image generated by {user_name}",
                description=request_data['prompt'],
                color=user_color
            )

            # Add resolution field
            if request_data['upscale_factor'] > 1:
                if request_data['upscaled_resolution'] and request_data['upscaled_resolution'] != "Unknown":
                    embed.add_field(
                        name="Resolution",
                        value=f"{request_data['resolution']} → {request_data['upscaled_resolution']} (CR Upscaled {request_data['upscale_factor']}x)",
                        inline=True
                    )
                else:
                    embed.add_field(
                        name="Resolution",
                        value=f"{request_data['resolution']} (CR Upscaled {request_data['upscale_factor']}x)",
                        inline=True
                    )
            else:
                embed.add_field(name="Resolution", value=request_data['resolution'], inline=True)

            # Handle LoRA information
            if isinstance(request_item, (ReduxRequestItem, ReduxPromptRequestItem)):
                # Skip LoRA display for Redux requests
                pass
            else:
                # Show LoRAs for both standard and PuLID requests
                lora_config = load_json('lora.json')
                lora_names = []
                if request_data['loras']:
                    for lora_file in request_data['loras']:
                        lora_info = next((lora for lora in lora_config['available_loras'] 
                                        if lora['file'] == lora_file), None)
                        if lora_info:
                            lora_names.append(lora_info['name'])
                        else:
                            lora_names.append(lora_file)

                embed.add_field(
                    name="LoRAs",
                    value=", ".join(lora_names) if lora_names else "None",
                    inline=True
                )

            if request_data['seed'] is not None:
                embed.add_field(name="Seed", value=str(request_data['seed']), inline=True)

            # Generate image filename and create file
            image_filename = f"generated_image_{request_data['request_id']}.png"
            image_file = discord.File(io.BytesIO(request_data['image_data']), image_filename)

            # Select appropriate view based on request type
            if isinstance(request_item, (ReduxRequestItem, ReduxPromptRequestItem)):
                view = ReduxImageView()
            elif request_item.workflow_filename and request_item.workflow_filename.lower().startswith('pulid'):
                view = PuLIDImageView()
            else:
                view = ImageControlView(
                    request.app['bot'],
                    request_data['prompt'],
                    image_filename,
                    request_data['resolution'],
                    request_data['loras'],
                    request_data['upscale_factor'],
                    request_data['seed']
                )

            # Update the original message
            channel = await request.app['bot'].fetch_channel(int(request_data['channel_id']))
            original_message = await channel.fetch_message(int(request_data['original_message_id']))
            await original_message.edit(content=None, embed=embed, attachments=[image_file], view=view)
            request.app['bot'].add_view(view, message_id=original_message.id)

            # Add to history
            add_to_history(
                request_data['user_id'],
                request_data['prompt'],
                None,  # workflow
                image_filename,
                request_data['resolution'],
                request_data['loras'],
                request_data['upscale_factor']
            )

            # Remove from pending requests
            if request_data['request_id'] in request.app['bot'].pending_requests:
                del request.app['bot'].pending_requests[request_data['request_id']]

            logger.info(f"Successfully processed image for user {request_data['user_id']}")
            return web.Response(text="Success")

        except discord.NotFound:
            logger.error("Channel or message not found")
            return web.Response(text="Channel or message not found", status=404)
        except discord.Forbidden:
            logger.error("Bot lacks required permissions")
            return web.Response(text="Permission denied", status=403)
        except Exception as e:
            logger.error(f"Error updating message: {str(e)}")
            return web.Response(text=f"Error updating message: {str(e)}", status=500)

    except Exception as e:
        logger.error(f"Error in handle_generated_image: {str(e)}", exc_info=True)
        return web.Response(text=f"Internal server error: {str(e)}", status=500)

async def update_progress(request):
    try:
        data = await request.json()
        request_id = data.get('request_id')
        progress_data = data.get('progress_data', {})
        
        if not request_id:
            return web.Response(text="Missing request_id", status=400)
            
        if request_id not in request.app['bot'].pending_requests:
            return web.Response(text="Unknown request_id", status=404)
            
        request_item = request.app['bot'].pending_requests[request_id]
        await update_progress_message(request.app['bot'], request_item, progress_data)
        
        return web.Response(text="Progress updated")
    except Exception as e:
        logger.error(f"Error in update_progress: {str(e)}", exc_info=True)
        return web.Response(text="Internal server error", status=500)

async def update_progress_message(bot, request_item, progress_data: Dict[str, Any]):
    try:
        channel = await bot.fetch_channel(int(request_item.channel_id))
        message = await channel.fetch_message(int(request_item.original_message_id))
        
        status = progress_data.get('status', '')
        progress_message = progress_data.get('message', 'Processing...')
        progress = progress_data.get('progress', 0)

        # Get the status info from our mapping
        status_info = STATUS_MESSAGES.get(status, {
            'message': progress_message,
            'emoji': '⚙️'  # Default emoji for unknown status
        })

        # Format message based on status
        if status == 'generating':
            formatted_message = f"{status_info['emoji']} {status_info['message']} {progress}%"
        elif status == 'error':
            formatted_message = f"{status_info['emoji']} {status_info['message']} {progress_message}"
        else:
            formatted_message = f"{status_info['emoji']} {status_info['message']}"

        await message.edit(content=formatted_message)
        logger.debug(f"Updated progress message: {formatted_message}")
        
    except discord.errors.NotFound:
        logger.warning(f"Message {request_item.original_message_id} not found")
    except discord.errors.Forbidden:
        logger.warning("Bot lacks permission to edit message")
    except Exception as e:
        logger.error(f"Error updating progress message: {str(e)}")

async def check_timeout(bot, request_id: str, timeout: int = 300):
    """
    Monitor request for timeout
    """
    try:
        await asyncio.sleep(timeout)
        if request_id in bot.pending_requests:
            request_item = bot.pending_requests[request_id]
            try:
                channel = await bot.fetch_channel(int(request_item.channel_id))
                message = await channel.fetch_message(int(request_item.original_message_id))
                await message.edit(content="⚠️ Generation timed out after 5 minutes")
            except Exception as e:
                logger.error(f"Error handling timeout: {str(e)}")
            finally:
                del bot.pending_requests[request_id]
    except Exception as e:
        logger.error(f"Error in timeout checker: {str(e)}")