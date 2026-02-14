import json
import os
import boto3
from datetime import datetime
from facebook_layer.facebook_service import FacebookService
from response_layer import response_helper
from tt_layer import token_tracking


def lambda_handler(event, context):
    # Initialize the Facebook service
    fb_service = FacebookService()
    events_client = boto3.client('events')
    
    try:
        # Check if the event is from API Gateway
        if 'httpMethod' in event:
            return handle_api_gateway_request(event, fb_service)
        # Handle direct Lambda invocation from Step Functions
        else:
            return handle_step_function_request(event, fb_service)
            
    except Exception as e:
        return response_helper.create_error_response(str(e))


def handle_api_gateway_request(event, fb_service):
    """Handle requests coming from API Gateway"""
    http_method = event['httpMethod']
    path = event['path']
    
    if path == '/get-access-token' and http_method == 'POST':
        body = json.loads(event['body'])
        auth_code = body.get('auth_code')
        redirect_uri = body.get('redirect_uri')
        result = fb_service.get_user_access_token(auth_code, redirect_uri)
        return response_helper.create_response(result)
        
    elif path == '/extend-token' and http_method == 'POST':
        body = json.loads(event['body'])
        short_lived_token = body.get('token')
        result = fb_service.extend_user_access_token(short_lived_token)
        return response_helper.create_response(result)

    elif path == '/get_page_info' and http_method == 'GET':
        params = event.get('queryStringParameters', {})

        user_access_token = params.get('userToken')
        page_id = params.get('pageId')
        
        pages_data = fb_service.get_facebook_pages(user_access_token)
        
        if not isinstance(pages_data, list):  # Ensure response is a list before iterating
            return {"error": "Failed to retrieve pages data"}
        
        page_info = fb_service.extract_page_info(pages_data, page_id)
        
        return response_helper.create_response(page_info)    
        
    elif path == '/get-pages' and http_method == 'GET':
        # user_access_token = event['queryStringParameters'].get('access_token')
        # result = fb_service.get_facebook_pages(user_access_token)
        # return response_helper.create_response(result)
        user_access_token = event['queryStringParameters'].get('access_token')
        result = fb_service.get_facebook_pages(user_access_token)
        
        # Initialize token tracking
        token_tracker = token_tracking.TokenTracking()
        
        # If result contains page data, fetch token tracking data for each page
        if isinstance(result, list):
            for page in result:
                page_id = page.get('id')
                if page_id:
                    try:
                        # Get token tracking data for this page
                        tracking_data = token_tracker.get_page_content(page_id)
                        print(f'TRACKING_DATA: {tracking_data}')
                        # Add tracking data to page object
                        page['token_tracking'] = {
                            'generated_item': tracking_data.get('generated_item', []),
                            'total_tokens': sum(
                                prompt['total_tokens'] for prompt in tracking_data.get('generated_item', [])
                            )
                        }
                    except Exception as e:
                        # If there's an error getting tracking data, add empty tracking data
                        page['token_tracking'] = {
                            'generated_item': [],
                            'total_tokens': 0,
                            'error': str(e)
                        }
        
        return response_helper.create_response(result)

    elif path == '/post-to-page' and http_method == 'POST':
        body = json.loads(event['body'])
        page_id = body.get('page_id')
        page_access_token = body.get('page_access_token')
        message = body.get('message')
        requires_image = body.get('requiresImage', False)
        image_url = body.get('image_url', None)
        social_media = body.get('social_media', 'Facebook')  

        if not page_id or not page_access_token or not message:
            return response_helper.create_error_response("Missing required parameters", 400)    
        
        social_media = body.get('social_media', 'Facebook')  # Default to Facebook

        if social_media == 'Instagram':
            instagram_id = body.get('instagram_id')
            if not instagram_id:
                return response_helper.create_error_response("Missing instagram_id parameter", 400)
            result = fb_service.post_to_instagram(instagram_id, page_access_token, message, image_url)
        else:
            result = fb_service.post_to_facebook_page(page_id, page_access_token, message, requires_image, image_url)

        return response_helper.create_response(result)

    elif path == '/get-page-feed' and http_method == 'GET':
        # Get parameters from query string
        params = event.get('queryStringParameters', {})
        page_id = params.get('page_id')
        page_access_token = params.get('page_access_token')
        limit = params.get('limit', 25)  # Default to 25 if not specified
        fields = params.get('fields' , None)  # Optional parameter

        if not page_id or not page_access_token:
            return response_helper.create_error_response("Missing required parameters: page_id and page_access_token", 400)

        result = fb_service.get_page_feed(page_id, page_access_token, limit=int(limit), fields=fields)
        return response_helper.create_response(result)  

    elif path == '/reply-to-comment' and http_method == 'POST':
        try:
            # Parse the request body
            body = json.loads(event['body'])
            
            # Extract required parameters
            original_comment_id = body.get('original_comment_id')
            page_access_token = body.get('page_access_token')
            reply_text = body.get('reply_text')
            
            # Validate required parameters
            if not original_comment_id:
                return response_helper.create_error_response("Missing original_comment_id parameter", 400)
            if not page_access_token:
                return response_helper.create_error_response("Missing page_access_token parameter", 400)
            if not reply_text:
                return response_helper.create_error_response("Missing reply_text parameter", 400)
            
            # Call the service to reply to the comment
            result = fb_service.reply_to_comment(original_comment_id, page_access_token, reply_text)
            return response_helper.create_response(result)
            
        except json.JSONDecodeError:
            return response_helper.create_error_response("Invalid JSON in request body", 400)
        except Exception as e:
            return response_helper.create_error_response(f"Error processing comment reply: {str(e)}", 500)

    elif path == '/webhook' and http_method == 'GET':
        # Get query parameters for webhook verification
        params = event.get('queryStringParameters', {})
        
        # Required verification parameters from Meta
        hub_mode = params.get('hub.mode')
        hub_verify_token = params.get('hub.verify_token')
        hub_challenge = params.get('hub.challenge')

        print(f'WEBHOOK_GET: {params}')
        
        # Verify the webhook
        if hub_mode == 'subscribe' and hub_verify_token:
            verify_result = fb_service.verify_webhook(hub_verify_token)
            if verify_result:
                return {
                    'statusCode': 200,
                    'body': hub_challenge
                }
        
        return response_helper.create_error_response('Webhook verification failed', 403)  
            
    elif path == '/webhook' and http_method == 'POST':
        try:
            # Parse the incoming webhook payload
            payload = json.loads(event['body'])
            
            # Process the webhook event and get event_info
            processed_events = fb_service.process_webhook_event(payload)

            print(f'PROCESSED_EVENT: {processed_events}')
            
            # Publish each event to EventBridge
            for event_info in processed_events:
                event_info['action'] = "generate_comment_reply"
                fb_service.publish_to_eventbridge(event_info)
            
            # Return 200 OK to acknowledge receipt
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'processed_events': len(processed_events)
                })
            }
        except Exception as e:
            return response_helper.create_error_response(f"Error processing webhook: {str(e)}", 500)

    elif path == '/page-subscriptions' and http_method == 'GET':
        try:
            params = event.get('queryStringParameters', {})
            page_id = params.get('page_id')
            page_access_token = params.get('page_access_token')
            
            if not page_id or not page_access_token:
                return response_helper.create_error_response("Missing required parameters: page_id and page_access_token", 400)
            
            result = fb_service.get_page_subscriptions(page_id, page_access_token)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error getting page subscriptions: {str(e)}", 500)

    elif path == '/subscribe-to-page' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            page_id = body.get('page_id')
            page_access_token = body.get('page_access_token')
            fields = body.get('fields')  # Optional parameter
            
            if not page_id or not page_access_token:
                return response_helper.create_error_response("Missing required parameters: page_id and page_access_token", 400)
            
            result = fb_service.subscribe_app_to_page(page_id, page_access_token, fields)
            return response_helper.create_response(result)
        except json.JSONDecodeError:
            return response_helper.create_error_response("Invalid JSON in request body", 400)
        except Exception as e:
            return response_helper.create_error_response(f"Error subscribing to page: {str(e)}", 500)

    elif path == '/unsubscribe-from-page' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            page_id = body.get('page_id')
            page_access_token = body.get('page_access_token')
            fields_to_remove = body.get('fields')  # Can be string or list of fields
            
            if not page_id or not page_access_token:
                return response_helper.create_error_response("Missing required parameters: page_id and page_access_token", 400)
            
            if not fields_to_remove:
                return response_helper.create_error_response("Missing required parameter: fields", 400)
            
            result = fb_service.unsubscribe_app_from_page_fields(page_id, page_access_token, fields_to_remove)
            return response_helper.create_response(result)
        except json.JSONDecodeError:
            return response_helper.create_error_response("Invalid JSON in request body", 400)
        except Exception as e:
            return response_helper.create_error_response(f"Error unsubscribing from page: {str(e)}", 500)        

    elif path == '/send-message' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            recipient_id = body.get('recipient_id')
            message_text = body.get('message_text')
            page_access_token = body.get('page_access_token')
            
            if not recipient_id or not message_text or not page_access_token:
                return response_helper.create_error_response("Missing required parameters", 400)
            
            result = fb_service.send_message(recipient_id, message_text, page_access_token)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error sending message: {str(e)}", 500)

    elif path == '/send-message-attachment' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            recipient_id = body.get('recipient_id')
            attachment_type = body.get('attachment_type')
            attachment_url = body.get('attachment_url')
            page_access_token = body.get('page_access_token')
            
            if not all([recipient_id, attachment_type, attachment_url, page_access_token]):
                return response_helper.create_error_response("Missing required parameters", 400)
            
            result = fb_service.send_message_with_attachment(recipient_id, attachment_type, attachment_url, page_access_token)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error sending attachment: {str(e)}", 500)

    elif path == '/send-quick-reply' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            recipient_id = body.get('recipient_id')
            message_text = body.get('message_text')
            quick_replies = body.get('quick_replies', [])
            page_access_token = body.get('page_access_token')
            
            if not all([recipient_id, message_text, page_access_token]):
                return response_helper.create_error_response("Missing required parameters", 400)
            
            result = fb_service.send_quick_reply_message(recipient_id, message_text, quick_replies, page_access_token)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error sending quick reply: {str(e)}", 500)

    elif path == '/get-user-profile' and http_method == 'GET':
        try:
            params = event.get('queryStringParameters', {})
            user_id = params.get('user_id')
            page_access_token = params.get('page_access_token')
            fields = params.get('fields')
            
            if not user_id or not page_access_token:
                return response_helper.create_error_response("Missing required parameters", 400)
            
            result = fb_service.get_user_profile(user_id, page_access_token, fields)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error getting user profile: {str(e)}", 500)

    elif path == '/set-typing' and http_method == 'POST':
        try:
            body = json.loads(event['body'])
            recipient_id = body.get('recipient_id')
            action = body.get('action', 'typing_on')  # Default to typing_on
            page_access_token = body.get('page_access_token')
            
            if not recipient_id or not page_access_token:
                return response_helper.create_error_response("Missing required parameters", 400)
            
            if action not in ['typing_on', 'typing_off']:
                return response_helper.create_error_response("Invalid action. Use 'typing_on' or 'typing_off'", 400)
            
            result = fb_service.set_typing_indicator(recipient_id, action, page_access_token)
            return response_helper.create_response(result)
        except Exception as e:
            return response_helper.create_error_response(f"Error setting typing indicator: {str(e)}", 500)
            
    else:
        return response_helper.create_error_response('Invalid path or HTTP method', 404)

def handle_step_function_request(event, fb_service):
    """Handle direct invocations from Step Functions"""
    action = event.get('action')
    
    if action == 'get_pages':
        user_access_token = event.get('userToken')
        result = fb_service.get_facebook_pages(user_access_token)
        return result

    elif action == 'get_page_info':
        user_access_token = event.get('userToken')
        page_id = event.get('pageId')
        
        pages_data = fb_service.get_facebook_pages(user_access_token)
        
        if not isinstance(pages_data, list):  # Ensure response is a list before iterating
            return {"error": "Failed to retrieve pages data"}
        
        page_info = fb_service.extract_page_info(pages_data, page_id)

        # result = fb_service.get_page_data(page_id , page_info["access_token"])
        return page_info    

    elif action == 'post_to_page':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        message = event.get('message')
        mediaType = event.get('mediaType', False)
        mm_url = event.get('mm_url', None)

        if not page_id or not page_access_token or not message:
            return {"error": "Missing required parameters"}

        social_media = event.get('social_media', 'Facebook')

        if social_media == 'Instagram':
            instagram_id = page_id
            if not instagram_id:
                return {"error": "Missing required parameter: instagram_id"}
            result = fb_service.post_to_instagram(instagram_id, page_access_token, message, mediaType, mm_url)
        else:
            result = fb_service.post_to_facebook_page(page_id, page_access_token, message, mediaType, mm_url)

        return result

    # Add these action handlers to your existing Lambda handler

    elif action == 'post_to_page':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        message = event.get('message')
        mediaType = event.get('mediaType', False)
        mm_url = event.get('mm_url', None)
        if not page_id or not page_access_token or not message:
            return {"error": "Missing required parameters"}
        
        social_media = event.get('social_media', 'Facebook')
        if social_media == 'Instagram':
            instagram_id = page_id
            if not instagram_id:
                return {"error": "Missing required parameter: instagram_id"}
            # For Instagram, redirect to state machine workflow
            return {
                "redirect_to_state_machine": True,
                "message": "Instagram posts must use state machine workflow. Use 'create_instagram_media' action instead."
            }
        else:
            result = fb_service.post_to_facebook_page(page_id, page_access_token, message, mediaType, mm_url)
        return result

    # NEW ACTION HANDLERS FOR STATE MACHINE

    elif action == 'create_instagram_media':
        instagram_id = event.get('instagram_id') or event.get('page_id')
        page_access_token = event.get('page_access_token')
        caption = event.get('caption') or event.get('message')
        mediaType = event.get('mediaType')
        mm_url = event.get('mm_url')
        
        if not instagram_id or not page_access_token or not caption:
            return {"error": "Missing required parameters: instagram_id, page_access_token, caption"}
        
        result = fb_service.create_instagram_media(
            instagram_id, page_access_token, caption, mediaType, mm_url
        )
        
        # Enrich result with parameters needed for next steps
        if result.get('status') == 'created':
            result['instagram_id'] = instagram_id
            result['page_access_token'] = page_access_token
        
        return result

    elif action == 'check_instagram_media_status':
        creation_id = event.get('creation_id')
        page_access_token = event.get('page_access_token')
        
        if not creation_id or not page_access_token:
            return {"error": "Missing required parameters: creation_id, page_access_token"}
        
        result = fb_service.check_instagram_media_status(creation_id, page_access_token)
        
        # Pass through parameters needed for next steps
        result['creation_id'] = creation_id
        result['instagram_id'] = event.get('instagram_id')
        result['page_access_token'] = page_access_token
        result['media_type'] = event.get('media_type')
        result['attempt'] = event.get('attempt', 0) + 1
        
        return result

    elif action == 'publish_instagram_media':
        instagram_id = event.get('instagram_id')
        creation_id = event.get('creation_id')
        page_access_token = event.get('page_access_token')
        
        if not instagram_id or not creation_id or not page_access_token:
            return {"error": "Missing required parameters: instagram_id, creation_id, page_access_token"}
        
        result = fb_service.publish_instagram_media(instagram_id, creation_id, page_access_token)
        
        # Pass through parameters for retry logic
        if result.get('status') == 'not_ready':
            result['creation_id'] = creation_id
            result['instagram_id'] = instagram_id
            result['page_access_token'] = page_access_token
            result['media_type'] = event.get('media_type')
            result['publish_attempt'] = event.get('publish_attempt', 0) + 1
        
        return result    

    elif action == 'post_reel':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        description = event.get('message')
        video_url = event.get('mm_url')
        
        if not page_id or not page_access_token or not description or not video_url:
            return {"error": "Missing required parameters: page_id, page_access_token, description, or video_url"}
            
        result = fb_service.post_reel(page_id, page_access_token, description, video_url)
        return result    

    elif action == 'init_reel_upload':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        description = event.get('message')
        video_url = event.get('mm_url')
        platform = event.get('platform')
        
        if not page_id or not page_access_token or not description or not video_url:
            return {"error": "Missing required parameters: page_id, page_access_token, description, or video_url"}
            
        result = fb_service.init_reel_upload(page_id, page_access_token, description, video_url, platform)
        return result
        
    elif action == 'upload_hosted_file':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        video_id = event.get('video_id')
        file_url = event.get('mm_url')
        platform = event.get('platform')
        
        if not page_id or not page_access_token or not video_id or not file_url:
            return {"error": "Missing required parameters: page_id, page_access_token, video_id, or file_url"}
            
        result = fb_service.upload_hosted_file(page_id, page_access_token, video_id, file_url, platform)
        return result

    elif action == 'check_reel_upload_status':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        video_id = event.get('video_id')
        platform = event.get('platform')
        
        if not page_id or not page_access_token or not video_id:
            return {"error": "Missing required parameters: page_id, page_access_token, or video_id"}
            
        result = fb_service.check_reel_upload_status(page_id, page_access_token, video_id, platform)
        return result
        
    elif action == 'publish_reel':
        print(f"REQUEST: {event}")
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        video_id = event.get('video_id')
        description = event.get('message')
        share_to_feed = event.get('share_to_feed', True)
        audio_name = event.get('audio_name')
        thumbnail_url = event.get('thumbnail_url')
        platform = event.get('platform')
        
        if not page_id or not page_access_token or not video_id or not description:
            return {"error": "Missing required parameters: page_id, page_access_token, video_id, or description"}
            
        result = fb_service.publish_reel(
            page_id, 
            page_access_token, 
            video_id, 
            description,
            platform,
            share_to_feed,
            audio_name,
            thumbnail_url
        )
        return result    

    elif action == 'create_live_stream':
        print(f"DEBUG - Raw event: {event}")
        
        try:
            print(f"DEBUG - Starting create_live_stream with event: {event}")
            
            # Check and extract required parameters
            page_id = event.get('page_id', '') 
            print(f"DEBUG - page_id: {page_id}")
            
            page_access_token = event.get('page_access_token')
            print(f"DEBUG - page_access_token exists: {bool(page_access_token)}")
            if page_access_token:
                # Only print first few characters for security
                print(f"DEBUG - page_access_token prefix: {page_access_token[:5]}...")
            
            # Extract live_stream_data properly first before trying to use it
            live_stream_data = event.get('live_stream_data')
            print(f"DEBUG - live_stream_data type: {type(live_stream_data)}")
            
            # JSON parsing with error handling
            try:
                # Handle the case where live_stream_data might already be a dict
                if isinstance(live_stream_data, dict):
                    live_stream_data_json = live_stream_data
                else:
                    # If it's a string, parse it as JSON
                    live_stream_data_json = json.loads(live_stream_data) if live_stream_data else {}
                
                print(f"DEBUG - live_stream_data_json: {live_stream_data_json}")
                
                # Extract title from the parsed JSON
                title = live_stream_data_json.get('title')
                print(f"DEBUG - title: {title}")
            except Exception as e:
                print(f"ERROR - Failed to parse live_stream_data: {str(e)}")
                title = None
                live_stream_data_json = {}
            
            # Parameter validation
            if not page_id:
                print("ERROR - Missing page_id")
                return {"error": "Missing required parameter: page_id"}
            if not page_access_token:
                print("ERROR - Missing page_access_token")
                return {"error": "Missing required parameter: page_access_token"}
            if not title:
                print("ERROR - Missing title")
                return {"error": "Missing required parameter: title"}
            
            print("DEBUG - All parameters validated, calling fb_service.create_live_stream")
            
            # Call the service function with try/except
            try:
                result = fb_service.create_live_stream(
                    page_id=page_id,
                    page_access_token=page_access_token,
                    title=title,
                    description=event.get('stream_description', title) 
                )
                print(f"DEBUG - create_live_stream result: {result}")
                return result
            except Exception as e:
                print(f"ERROR - fb_service.create_live_stream failed: {str(e)}")
                import traceback
                print(f"ERROR - Traceback: {traceback.format_exc()}")
                return {"error": f"Failed to create live stream: {str(e)}"}
                
        except Exception as e:
            print(f"ERROR - Unexpected error in create_live_stream: {str(e)}")
            import traceback
            print(f"ERROR - Traceback: {traceback.format_exc()}")
            return {"error": f"Unexpected error: {str(e)}"}
    
    elif action == 'extend_token':
        short_lived_token = event.get('token')
        result = fb_service.extend_user_access_token(short_lived_token)
        return result
        
    elif action == 'get_access_token':
        auth_code = event.get('authCode')
        redirect_uri = event.get('redirectUri')
        result = fb_service.get_user_access_token(auth_code, redirect_uri)
        return result

    elif action == 'get_page_feed':
        page_id = event.get('page_id')
        page_access_token = event.get('page_access_token')
        limit = event.get('limit', 25)  # Default to 25 if not specified
        fields = event.get('fields')  # Optional parameter

        if not page_id or not page_access_token:
            return {"error": "Missing required parameters: page_id and page_access_token"}

        result = fb_service.get_page_feed(page_id, page_access_token, limit=limit, fields=fields)
        return result    

    elif action == 'reply_to_comment':
        # Extract required parameters
        original_comment_id = event.get('original_comment_id')
        page_access_token = event.get('page_access_token')
        reply_text = event.get('reply_text')
        commenter_id = event.get('commenter_id')
        
        # Validate required parameters
        if not original_comment_id or not page_access_token or not reply_text:
            return {
                "status": "error",
                "error_details": "Missing required parameters",
                "timestamp": datetime.now().isoformat()
            }
        
        # Call the service to reply to the comment
        return fb_service.reply_to_comment(original_comment_id, page_access_token, reply_text, commenter_id)    

    elif action == 'send_message':
        recipient_id = event.get('recipient_id')
        message_text = event.get('message_text')
        page_access_token = event.get('page_access_token')
        
        if not recipient_id or not message_text or not page_access_token:
            return {"error": "Missing required parameters"}
        
        result = fb_service.send_message(recipient_id, message_text, page_access_token)
        return result

    elif action == 'send_message_attachment':
        recipient_id = event.get('recipient_id')
        attachment_type = event.get('attachment_type')
        attachment_url = event.get('attachment_url')
        page_access_token = event.get('page_access_token')
        
        if not all([recipient_id, attachment_type, attachment_url, page_access_token]):
            return {"error": "Missing required parameters"}
        
        result = fb_service.send_message_with_attachment(recipient_id, attachment_type, attachment_url, page_access_token)
        return result

    elif action == 'get_user_profile':
        user_id = event.get('user_id')
        page_access_token = event.get('page_access_token')
        fields = event.get('fields')
        
        if not user_id or not page_access_token:
            return {"error": "Missing required parameters"}
        
        result = fb_service.get_user_profile(user_id, page_access_token, fields)
        return result

    elif action == 'get_instagram_profile':
        instagram_id = event.get('instagram_id')
        page_access_token = event.get('page_access_token')
        
        if not instagram_id or not page_access_token:
            return {"error": "Missing required parameters: instagram_id and page_access_token"}
        
        result = fb_service.get_instagram_profile_details(instagram_id, page_access_token)
        return result

    else:
        raise ValueError(f"Invalid action: {action}")
  

