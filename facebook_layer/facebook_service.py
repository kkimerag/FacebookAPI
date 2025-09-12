import json
import boto3
import requests
import time
from urllib.parse import urlparse
from datetime import datetime
from botocore.exceptions import ClientError

class FacebookService:
    def __init__(self):
        self.secrets_client = boto3.client('secretsmanager')
        self.events_client = boto3.client('events')
        self._load_secrets()

    def _load_secrets(self):
        try:
            response = self.secrets_client.get_secret_value(
                SecretId='facebook/credentials'
            )
            secrets = json.loads(response['SecretString'])
            self.app_id = secrets['app_id']
            self.app_secret = secrets['app_secret']
            self.webhook_verify_token = secrets['webhook_verify_token']
        except ClientError as e:
            raise Exception(f"Failed to load secrets: {str(e)}")

    def extract_stream_details(self, stream_url):
        """
        Extract server URL and stream key from the full stream URL as provided by Facebook Live API.
        
        :param stream_url: The full stream URL (e.g., rtmps://live-api-s.facebook.com:443/rtmp/123456789?s_bl=1&...)
        :return: Tuple of (server_url, stream_key)
        :raises ValueError: If the URL format is invalid
        """
        # Parse the URL
        parsed = urlparse(stream_url)
        
        # Extract the path and query components
        path = parsed.path
        query = parsed.query
        
        # Facebook Live Producer format requires:
        # - Server URL: rtmps://live-api-s.facebook.com:443/rtmp/
        # - Stream Key: [ID]?[query parameters]
        
        if not path.startswith('/rtmp/'):
            raise ValueError("Invalid stream URL format: missing /rtmp/ path prefix")
        
        # Extract the ID portion from the path (remove /rtmp/ prefix)
        stream_id = path[len('/rtmp/'):]
        
        # Construct the stream key by combining the ID and query parameters
        stream_key = f"{stream_id}?{query}" if query else stream_id
        
        # The server URL is the base URL with /rtmp/ path
        server_url = f"{parsed.scheme}://{parsed.netloc}/rtmp"
        
        return server_url, stream_key

    def create_live_stream(self, page_id, page_access_token, title=None, description=None):
        """
        Create a live stream on the specified Facebook page.
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page with necessary permissions
        :param title: Optional title for the live stream
        :param description: Optional description for the live stream
        :return: Dictionary containing the server URL, stream key, and backup stream key
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/live_videos"
        params = {
            "access_token": page_access_token,
            "status": "LIVE_NOW",
            "enable_backup_ingest": True
        }
        if title:
            params["title"] = title
        if description:
            params["description"] = description
        
        try:
            response = requests.post(url, data=params)
            print(f'RAW_RESPONSE: {response}')
            if response.ok:
                data = response.json()
                if 'id' in data and 'stream_url' in data:
                    server_url, stream_key = self.extract_stream_details(data['stream_url'])
                    backup_stream_key = None
                    
                    # Handle backup stream URL, which is now in stream_secondary_urls array
                    if 'stream_secondary_urls' in data and len(data['stream_secondary_urls']) > 0:
                        _, backup_stream_key = self.extract_stream_details(data['stream_secondary_urls'][0])
                    
                    return {
                        "live_video_id": data['id'],
                        "server_url": server_url,
                        "stream_key": stream_key,
                        "backup_stream_key": backup_stream_key
                    }
                else:
                    return {"error": data.get('error', 'Unknown error')}
            else:
                return {"error": response.text}
        except Exception as e:
            return {"error": str(e)}

    def verify_webhook(self, verify_token):
            """
            Verify the webhook verification token from Meta
            
            :param verify_token: The verification token received from Meta
            :return: Boolean indicating if verification was successful
            """
            return verify_token == self.webhook_verify_token    
            
    def process_webhook_event(self, payload):
        """
        Process incoming webhook events from Meta
        
        :param payload: The JSON payload from the webhook
        :return: Processing result information and event_info for EventBridge
        """
        # Verify that this is a page webhook event
        if 'object' not in payload or payload['object'] != 'page':
            raise ValueError("Received webhook is not for a page")
        
        processed_events = []
        
        # Process each entry in the webhook
        for entry in payload.get('entry', []):
            page_id = entry.get('id')
            
            # Process each change in the entry
            for change in entry.get('changes', []):
                field = change.get('field')
                value = change.get('value', {})
                
                event_info = self._process_feed_event(value, page_id)
                if event_info:
                    processed_events.append(event_info)
        
        return processed_events

    def publish_to_eventbridge(self, event_info):
        """
        Publish event_info to EventBridge
        
        :param event_info: The event information to publish
        :return: Response from EventBridge PutEvents
        """
        try:
            response = self.events_client.put_events(
                Entries=[
                    {
                        'Source': 'facebook.webhook',
                        'DetailType': 'Facebook Webhook Event',
                        'Detail': json.dumps(event_info),
                        'EventBusName': 'default'
                    }
                ]
            )
            return response
        except Exception as e:
            print(f"Error publishing to EventBridge: {str(e)}")
            raise

    def get_user_access_token(self, auth_code, redirect_uri):
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "redirect_uri": redirect_uri,
            "code": auth_code
        }
        response = requests.get(url, params=params)
        return response.json()

    def extend_user_access_token(self, short_lived_token):
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "fb_exchange_token": short_lived_token
        }
        response = requests.get(url, params=params)
        return response.json()

    def extend_page_access_token(self, page_access_token):
        """
        Extends a short-lived page access token to a long-lived one (valid for about 60 days)
        
        Args:
            page_access_token (str): The short-lived page access token to extend
            
        Returns:
            dict: JSON response containing the long-lived token and expiration
        """
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "fb_exchange_token": page_access_token,
            "access_type": "page"  # Specify that we want a page access token
        }
        response = requests.get(url, params=params)
        return response.json()    

    import requests

    def get_facebook_pages(self, user_access_token):
        url = "https://graph.facebook.com/v18.0/me/accounts"
        params = {
            "fields": "id,name,access_token,category,about,bio,description,story,fan_count,link,website,picture",
            "access_token": user_access_token
        }
        response = requests.get(url, params=params)
        data = response.json()
        
        if "data" not in data:
            return {"error": data}

        pages = data["data"]

        # Now fetch instagram account for each page
        for page in pages:
            page_token = page.get("access_token")
            page_id = page.get("id")

            ig_url = f"https://graph.facebook.com/v18.0/{page_id}"
            ig_params = {
                "fields": "instagram_business_account",
                "access_token": page_token  # must use PAGE token here
            }
            ig_response = requests.get(ig_url, params=ig_params).json()
            ig_account = ig_response.get("instagram_business_account")
            
            page["instagram_id"] = ig_account["id"] if ig_account else None

        return pages

    def get_page_data(self, page_id, page_access_token): #To be deleted
        url = f"https://graph.facebook.com/v18.0/{page_id}"
        params = {
            "fields": "id,name,category,about.limit(10000),bio,description",
            "access_token": page_access_token
        }
        response = requests.get(url, params=params)
        return response.json()            

    def post_to_facebook_page(self, page_id, page_access_token, message, mediaType=None, mm_url=None):
        """
        Posts to a Facebook page based on the mediaType parameter.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param message: The text content of the post
        :param mediaType: Type of media to post ('none', 'image', or 'video')
        :param mm_url: The URL of the image (required if mediaType is 'image')
        :param mm_url: The URL of the video (required if mediaType is 'video')
        :return: JSON response from the Facebook API
        """

        print(f'MEDIA_TYPE: {mediaType}')
        
        if mediaType == 'image' and mm_url:
            url = f"https://graph.facebook.com/v18.0/{page_id}/photos"
            params = {
                "message": message,
                "url": mm_url,
                "access_token": page_access_token
            }
        elif mediaType == 'video' and mm_url:
            url = f"https://graph.facebook.com/v18.0/{page_id}/videos"
            params = {
                "description": message,
                "file_url": mm_url,
                "access_token": page_access_token
            }
        else:
            # Default to text-only post if mediaType is 'none' or not specified
            url = f"https://graph.facebook.com/v18.0/{page_id}/feed"
            params = {
                "message": message,
                "access_token": page_access_token
            }
        
        response = requests.post(url, data=params)
        return response.json()

    def init_reel_upload(self, page_id, page_access_token, description, video_url):
        """
        Step 1: Initialize a reel upload with video URL
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param description: The description/caption for the reel
        :param video_url: The URL of the video to use for the reel
        :return: Dictionary with upload session details
        """
        try:
            # Initialize the upload session
            start_url = f"https://graph.facebook.com/v18.0/{page_id}/video_reels"
            start_params = {
                "upload_phase": "start",
                "access_token": page_access_token,
                "video_url": video_url
            }
            start_response = requests.post(start_url, data=start_params)
            start_result = start_response.json()
            
            if 'error' in start_result:
                return {
                    "status": "error",
                    "page_id": page_id,
                    "error_details": start_result['error'],
                    "phase": "start",
                    "timestamp": datetime.now().isoformat()
                }
            
            video_id = start_result.get('video_id')
            if not video_id:
                return {
                    "status": "error",
                    "page_id": page_id,
                    "error_details": "Missing video_id in start response",
                    "phase": "start",
                    "timestamp": datetime.now().isoformat()
                }
            
            # Store description and other details for later use
            return {
                "status": "pending",
                "page_id": page_id,
                "video_id": video_id,
                "description": description,
                "phase": "initialized",
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            import traceback
            return {
                "status": "error",
                "page_id": page_id,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
                "phase": "initialization",
                "timestamp": datetime.now().isoformat()
            }

    def upload_hosted_file(self, page_id, page_access_token, video_id, file_url):
        """
        Upload a hosted video file for a reel using the video ID from the initialization step.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param video_id: The ID of the video from the init_reel_upload step
        :param file_url: The URL of the hosted video file (must allow facebookexternalhit/1.1 user agent)
        :return: Dictionary with upload status
        """
        try:
            print(f"[DEBUG] Starting hosted file upload for video_id: {video_id}, page_id: {page_id}")
            print(f"[DEBUG] File URL: {file_url}")
            
            # Validate file_url
            if not file_url.startswith('https://'):
                print(f"[ERROR] File URL validation failed: {file_url} - Not using HTTPS protocol")
                return {
                    "status": "error",
                    "page_id": page_id,
                    "video_id": video_id,
                    "error_details": "File URL must use HTTPS protocol",
                    "phase": "upload_hosted_file",
                    "timestamp": datetime.now().isoformat()
                }
                
            # Check if the host is not a Meta CDN (fbcdn URLs are rejected)
            parsed_url = urlparse(file_url)
            print(f"[DEBUG] URL host: {parsed_url.netloc}")
            
            if 'fbcdn.net' in parsed_url.netloc.lower():
                print(f"[ERROR] Host validation failed: {parsed_url.netloc} - Meta CDN not supported")
                return {
                    "status": "error",
                    "page_id": page_id,
                    "video_id": video_id,
                    "error_details": "Files hosted on Meta CDN (fbcdn) are not supported. Use crossposting instead.",
                    "phase": "upload_hosted_file",
                    "timestamp": datetime.now().isoformat()
                }
                
            upload_url = f"https://rupload.facebook.com/video-upload/v22.0/{video_id}"
            print(f"[DEBUG] Upload endpoint: {upload_url}")
            
            headers = {
                "Authorization": f"OAuth {page_access_token}",
                "file_url": file_url
            }
            print(f"[DEBUG] Request headers: {headers}")
            
            # Print partially redacted token for debugging (security best practice)
            token_preview = page_access_token[:5] + "..." + page_access_token[-5:] if len(page_access_token) > 10 else "***masked***"
            print(f"[DEBUG] Using access token (partially redacted): {token_preview}")
            
            print("[DEBUG] Sending API request to Facebook...")
            response = requests.post(upload_url, headers=headers)
            print(f"[DEBUG] Response status code: {response.status_code}")
            print(f"[DEBUG] Response content: {response.text[:200]}..." if len(response.text) > 200 else f"[DEBUG] Response content: {response.text}")
            
            result = response.json()
            print(f"[DEBUG] Parsed JSON response: {result}")
            
            if result.get('success') is True:
                print(f"[SUCCESS] File upload successful for video_id: {video_id}")
                return {
                    "status": "success",
                    "page_id": page_id,
                    "video_id": video_id,
                    "phase": "file_uploaded",
                    "timestamp": datetime.now().isoformat()
                }
            else:
                print(f"[ERROR] Upload failed. Error details: {result.get('error', 'Unknown error')}")
                return {
                    "status": "error",
                    "page_id": page_id,
                    "video_id": video_id,
                    "error_details": result.get('error', 'Unknown error'),
                    "phase": "upload_hosted_file",
                    "timestamp": datetime.now().isoformat()
                }
                
        except requests.exceptions.RequestException as req_err:
            print(f"[ERROR] Request exception: {req_err}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] Request exception traceback: {trace}")
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": f"Request error: {str(req_err)}",
                "error_type": "request_exception",
                "traceback": trace,
                "phase": "upload_hosted_file",
                "timestamp": datetime.now().isoformat()
            }
        except ValueError as json_err:
            print(f"[ERROR] JSON parsing error: {json_err}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] JSON error traceback: {trace}")
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": f"JSON parsing error: {str(json_err)}",
                "error_type": "json_parsing_error", 
                "response_text": response.text if 'response' in locals() else "No response",
                "traceback": trace,
                "phase": "upload_hosted_file",
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            print(f"[ERROR] Unexpected exception: {e}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] Exception traceback: {trace}")
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": str(e),
                "traceback": trace,
                "phase": "upload_hosted_file",
                "timestamp": datetime.now().isoformat()
            }
 
    def check_reel_upload_status(self, page_id, page_access_token, video_id):
        """
        Step 2: Check if the uploaded video is ready
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param video_id: The ID of the video being processed
        :return: Dictionary with upload status
        """
        try:
            status_url = f"https://graph.facebook.com/v18.0/{video_id}"
            status_params = {
                "fields": "status",
                "access_token": page_access_token
            }
            status_response = requests.get(status_url, params=status_params)
            status_result = status_response.json()
            
            if 'error' in status_result:
                return {
                    "status": "error",
                    "page_id": page_id,
                    "video_id": video_id,
                    "error_details": status_result['error'],
                    "phase": "check_status",
                    "timestamp": datetime.now().isoformat()
                }
            
            if 'status' in status_result:
                video_status = status_result['status'].get('video_status')
                
                if video_status == 'ready':
                    return {
                        "status": "ready",
                        "page_id": page_id,
                        "video_id": video_id,
                        "phase": "video_ready",
                        "timestamp": datetime.now().isoformat()
                    }
                elif video_status == 'error':
                    return {
                        "status": "error",
                        "page_id": page_id,
                        "video_id": video_id,
                        "error_details": "Video processing failed",
                        "facebook_error": status_result['status'].get('error'),
                        "phase": "upload",
                        "timestamp": datetime.now().isoformat()
                    }
                else:
                    # Still processing
                    return {
                        "status": "processing",
                        "page_id": page_id,
                        "video_id": video_id,
                        "video_status": video_status,
                        "phase": "awaiting_ready",
                        "raw_status": status_result['status'],
                        "timestamp": datetime.now().isoformat()
                    }
            else:
                return {
                    "status": "unknown",
                    "page_id": page_id,
                    "video_id": video_id,
                    "raw_response": status_result,
                    "phase": "check_status",
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            import traceback
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
                "phase": "check_status",
                "timestamp": datetime.now().isoformat()
            }            

    def publish_reel(self, page_id, page_access_token, video_id, description, share_to_feed=True, audio_name=None, thumbnail_url=None):
        """
        Step 3: Publish the reel once the video is ready
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param video_id: The ID of the processed video
        :param description: The description/caption for the reel
        :param share_to_feed: Whether to share the reel to the page's feed (default: True)
        :param audio_name: Optional name of audio track used in the reel
        :param thumbnail_url: Optional URL for a custom thumbnail image
        :return: Dictionary with publish status
        """
        try:
            print(f"[DEBUG] Starting publish_reel for video_id: {video_id}, page_id: {page_id}")
            print(f"[DEBUG] Description length: {len(description)} characters")
            print(f"[DEBUG] Share to feed: {share_to_feed}")
            print(f"[DEBUG] Audio name provided: {'Yes' if audio_name else 'No'}")
            print(f"[DEBUG] Thumbnail URL provided: {'Yes' if thumbnail_url else 'No'}")
            
            # Construct API endpoint
            finish_url = f"https://graph.facebook.com/v18.0/{page_id}/video_reels"
            print(f"[DEBUG] Publishing endpoint: {finish_url}")
            
            # Construct parameters
            finish_params = {
                "upload_phase": "finish",
                "video_id": video_id,
                "description": description,
                "share_to_feed": "true" if share_to_feed else "false",
                "access_token": page_access_token,
                "video_state": "PUBLISHED"
            }
            
            # Add optional parameters if provided
            if audio_name:
                finish_params["audio_name"] = audio_name
                print(f"[DEBUG] Including audio_name: {audio_name}")
                
            if thumbnail_url:
                finish_params["thumbnail_url"] = thumbnail_url
                print(f"[DEBUG] Including thumbnail_url: {thumbnail_url}")
            
            # Print parameters for debugging (excluding sensitive information)
            safe_params = finish_params.copy()
            if 'access_token' in safe_params:
                token_preview = page_access_token[:5] + "..." + page_access_token[-5:] if len(page_access_token) > 10 else "***masked***"
                safe_params['access_token'] = token_preview
            
            print(f"[DEBUG] Request parameters: {safe_params}")
            
            # Send the API request
            print("[DEBUG] Sending publish request to Facebook API...")
            finish_response = requests.post(finish_url, data=finish_params)
            print(f"[DEBUG] Response status code: {finish_response.status_code}")
            print(f"[DEBUG] Response content: {finish_response.text[:200]}..." if len(finish_response.text) > 200 else f"[DEBUG] Response content: {finish_response.text}")
            
            # Parse the response
            print("[DEBUG] Parsing JSON response...")
            finish_result = finish_response.json()
            print(f"[DEBUG] Parsed response: {finish_result}")
            
            # Check for success - Modified to handle Facebook's actual response format
            if 'success' in finish_result and finish_result['success'] is True:
                post_id = finish_result.get('post_id', None)
                print(f"[SUCCESS] Reel publish initiated successfully! Post ID: {post_id}")
                
                return {
                    "status": "success",
                    "page_id": page_id,
                    "reel_id": post_id,
                    "video_id": video_id,
                    "message": finish_result.get('message'),
                    "share_to_feed": share_to_feed,
                    "phase": "published",
                    "timestamp": datetime.now().isoformat()
                }
            elif 'id' in finish_result:
                # Keep original path for backward compatibility
                print(f"[SUCCESS] Reel published successfully! Reel ID: {finish_result['id']}")
                print(f"[DEBUG] Permalink URL: {finish_result.get('permalink_url', 'Not provided')}")
                
                return {
                    "status": "success",
                    "page_id": page_id,
                    "reel_id": finish_result['id'],
                    "video_id": video_id,
                    "permalink_url": finish_result.get('permalink_url'),
                    "share_to_feed": share_to_feed,
                    "phase": "published",
                    "timestamp": datetime.now().isoformat()
                }
            else:
                error_details = finish_result.get('error', {})
                print(f"[ERROR] Publish failed. Error details: {error_details}")
                
                return {
                    "status": "error",
                    "page_id": page_id,
                    "video_id": video_id,
                    "error_details": error_details,
                    "phase": "publish",
                    "timestamp": datetime.now().isoformat()
                }
                
        except requests.exceptions.RequestException as req_err:
            print(f"[ERROR] Request exception during publish: {req_err}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] Request exception traceback: {trace}")
            
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": f"Request error: {str(req_err)}",
                "error_type": "request_exception",
                "traceback": trace,
                "phase": "publish",
                "timestamp": datetime.now().isoformat()
            }
            
        except ValueError as json_err:
            print(f"[ERROR] JSON parsing error during publish: {json_err}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] JSON error traceback: {trace}")
            
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": f"JSON parsing error: {str(json_err)}",
                "error_type": "json_parsing_error",
                "response_text": finish_response.text if 'finish_response' in locals() else "No response",
                "traceback": trace,
                "phase": "publish",
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            print(f"[ERROR] Unexpected exception during publish: {e}")
            import traceback
            trace = traceback.format_exc()
            print(f"[DEBUG] Exception traceback: {trace}")
            
            return {
                "status": "error",
                "page_id": page_id,
                "video_id": video_id,
                "error_details": str(e),
                "traceback": trace,
                "phase": "publish",
                "timestamp": datetime.now().isoformat()
            }
    
    def post_reel(self, page_id, page_access_token, description, video_url, share_to_feed=True, audio_name=None, thumbnail_url=None):
        """
        Posts a reel to a Facebook page using the three-step upload process with video URL.
        Now returns immediately with a recommendation to use the state machine approach.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param description: The description/caption for the reel
        :param video_url: The URL of the video to use for the reel
        :param share_to_feed: Whether to share the reel to the page's feed (default: True)
        :param audio_name: Optional name of audio track used in the reel
        :param thumbnail_url: Optional URL for a custom thumbnail image
        :return: Dictionary with initialization status
        """
        result = self.init_reel_upload(page_id, page_access_token, description, video_url)
        result["message"] = "Reel upload initiated. Please use the state machine approach (init_reel_upload, check_reel_upload_status, publish_reel) to handle the multi-step process."
        return result

    def get_page_feed(self, page_id, page_access_token, limit=25, fields=None):
        """
        Gets the feed (posts) from a Facebook page.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param limit: Number of posts to retrieve (default: 25)
        :param fields: Specific fields to retrieve (optional)
        :return: JSON response containing the page's feed
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/feed"
        
        if fields is None:
            fields = "id,message,created_time,full_picture,permalink_url,shares,reactions.summary(total_count),comments.summary(total_count)"
        
        params = {
            "access_token": page_access_token,
            "limit": limit,
            "fields": fields
        }
        
        response = requests.get(url, params=params)
        return response.json()

    def reply_to_comment(self, original_comment_id, page_access_token, reply_text, commenter_id=None):
        """
        Reply to a Facebook comment with optional commenter mention
        
        :param original_comment_id: ID of the comment to reply to
        :param page_access_token: The access token for the page
        :param reply_text: The reply message content
        :param commenter_id: ID of the commenter to mention (optional)
        :return: JSON response with status and details
        """
        print(f'COMENTER_ID: {commenter_id}')
        url = f"https://graph.facebook.com/v18.0/{original_comment_id}/comments"
        
        # Format message with @mention if commenter_id is provided
        message = reply_text
        if commenter_id:
            # Add the mention tag at the beginning of the message
            mention_tag = f"@[{commenter_id}]"
            message = f"{mention_tag} {reply_text}"
        
        params = {
            "message": message,
            "access_token": page_access_token
        }
        
        try:
            response = requests.post(url, data=params)
            response_data = response.json()
            
            # If the response contains an ID, the comment was posted successfully
            if 'id' in response_data:
                return {
                    "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],  # Truncate token for security
                    "reply_text": reply_text,
                    "mentioned_user": commenter_id if commenter_id else None,
                    "status": "success",
                    "original_comment_id": original_comment_id,
                    "reply_id": response_data.get('id'),
                    "timestamp": datetime.now().isoformat()
                }
            else:
                # Handle Facebook API error
                return {
                    "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],
                    "reply_text": reply_text,
                    "mentioned_user": commenter_id if commenter_id else None,
                    "status": "error",
                    "original_comment_id": original_comment_id,
                    "error_details": response_data.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            # Handle any exceptions during the API call
            return {
                "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],
                "reply_text": reply_text,
                "mentioned_user": commenter_id if commenter_id else None,
                "status": "error",
                "original_comment_id": original_comment_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def is_own_comment(self, commenter_id, page_id):
        """
        Determine if a comment was made by our own page
        
        :param commenter_id: ID of the user who made the comment
        :param page_id: ID of the page receiving the comment
        :return: Boolean indicating if the comment is from our own page
        """
        return commenter_id == page_id

    def send_message(self, recipient_id, message_text, page_access_token):
        """
        Send a text message to a user via Facebook Messenger
        
        :param recipient_id: The PSID (Page-Scoped ID) of the recipient
        :param message_text: The text message to send
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            if 'message_id' in result:
                return {
                    "status": "success",
                    "message_id": result['message_id'],
                    "recipient_id": recipient_id,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "status": "error",
                    "error_details": result.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def send_message_with_attachment(self, recipient_id, attachment_type, attachment_url, page_access_token):
        """
        Send a message with media attachment (image, video, audio, file)
        
        :param recipient_id: The PSID of the recipient
        :param attachment_type: Type of attachment ('image', 'video', 'audio', 'file')
        :param attachment_url: URL of the media file
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": attachment_type,
                    "payload": {"url": attachment_url}
                }
            },
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            if 'message_id' in result:
                return {
                    "status": "success",
                    "message_id": result['message_id'],
                    "recipient_id": recipient_id,
                    "attachment_type": attachment_type,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "status": "error",
                    "error_details": result.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def send_quick_reply_message(self, recipient_id, message_text, quick_replies, page_access_token):
        """
        Send a message with quick reply buttons
        
        :param recipient_id: The PSID of the recipient
        :param message_text: The text message to send
        :param quick_replies: List of quick reply options [{"title": "Option 1", "payload": "PAYLOAD_1"}, ...]
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        # Format quick replies for Facebook API
        formatted_quick_replies = []
        for reply in quick_replies:
            formatted_quick_replies.append({
                "content_type": "text",
                "title": reply["title"],
                "payload": reply["payload"]
            })
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "text": message_text,
                "quick_replies": formatted_quick_replies
            },
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            if 'message_id' in result:
                return {
                    "status": "success",
                    "message_id": result['message_id'],
                    "recipient_id": recipient_id,
                    "quick_replies_count": len(quick_replies),
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "status": "error",
                    "error_details": result.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def send_template_message(self, recipient_id, template_type, elements, page_access_token):
        """
        Send a structured template message (generic, button, etc.)
        
        :param recipient_id: The PSID of the recipient
        :param template_type: Type of template ('generic', 'button', 'list', etc.)
        :param elements: Template elements/content
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": template_type,
                        "elements": elements
                    }
                }
            },
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            if 'message_id' in result:
                return {
                    "status": "success",
                    "message_id": result['message_id'],
                    "recipient_id": recipient_id,
                    "template_type": template_type,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "status": "error",
                    "error_details": result.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def mark_message_as_seen(self, sender_id, page_access_token):
        """
        Mark a message as seen/read
        
        :param sender_id: The PSID of the sender
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": sender_id},
            "sender_action": "mark_seen"
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            return {
                "status": "success" if 'recipient_id' in result else "error",
                "sender_id": sender_id,
                "action": "mark_seen",
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def set_typing_indicator(self, recipient_id, action, page_access_token):
        """
        Set typing indicator (on/off)
        
        :param recipient_id: The PSID of the recipient
        :param action: 'typing_on' or 'typing_off'
        :param page_access_token: Access token for the page
        :return: JSON response from Facebook API
        """
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "sender_action": action
        }
        
        params = {"access_token": page_access_token}
        
        try:
            response = requests.post(url, json=payload, params=params)
            result = response.json()
            
            return {
                "status": "success" if 'recipient_id' in result else "error",
                "recipient_id": recipient_id,
                "action": action,
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def get_user_profile(self, user_id, page_access_token, fields=None):
        """
        Get user profile information
        
        :param user_id: The PSID of the user
        :param page_access_token: Access token for the page
        :param fields: Comma-separated string of fields to retrieve
        :return: JSON response with user profile data
        """
        if fields is None:
            fields = "first_name,last_name,profile_pic"
        
        url = f"https://graph.facebook.com/v18.0/{user_id}"
        
        params = {
            "fields": fields,
            "access_token": page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            result = response.json()
            
            if 'first_name' in result or 'id' in result:
                return {
                    "status": "success",
                    "user_profile": result,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "status": "error",
                    "error_details": result.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def process_messaging_webhook(self, payload):
        """
        Process incoming messaging webhook events
        
        :param payload: The JSON payload from the webhook
        :return: List of processed messaging events
        """
        if 'object' not in payload or payload['object'] != 'page':
            raise ValueError("Received webhook is not for a page")
        
        processed_events = []
        
        for entry in payload.get('entry', []):
            page_id = entry.get('id')
            
            # Process messaging events
            for messaging_event in entry.get('messaging', []):
                event_info = {
                    'page_id': page_id,
                    'timestamp': messaging_event.get('timestamp'),
                    'sender_id': messaging_event.get('sender', {}).get('id'),
                    'recipient_id': messaging_event.get('recipient', {}).get('id')
                }
                
                # Handle different types of messaging events
                if 'message' in messaging_event:
                    message = messaging_event['message']
                    event_info.update({
                        'event_type': 'message',
                        'message_id': message.get('mid'),
                        'message_text': message.get('text'),
                        'attachments': message.get('attachments', []),
                        'quick_reply': message.get('quick_reply')
                    })
                elif 'postback' in messaging_event:
                    postback = messaging_event['postback']
                    event_info.update({
                        'event_type': 'postback',
                        'postback_payload': postback.get('payload'),
                        'postback_title': postback.get('title')
                    })
                elif 'delivery' in messaging_event:
                    delivery = messaging_event['delivery']
                    event_info.update({
                        'event_type': 'delivery',
                        'delivered_messages': delivery.get('mids', []),
                        'watermark': delivery.get('watermark')
                    })
                elif 'read' in messaging_event:
                    read = messaging_event['read']
                    event_info.update({
                        'event_type': 'read',
                        'watermark': read.get('watermark')
                    })
                
                processed_events.append(event_info)
        
        return processed_events        

    # Modified method to use the new helper methods
    def _process_feed_event(self, value, page_id):
        """
        Process feed-related webhook events with enhanced context handling
        
        :param value: The value object from the webhook change
        :param page_id: The Facebook page ID receiving the webhook
        :return: Processed event information
        """
        event_info = {
            'item': value.get('item'),
            'verb': value.get('verb')
        }
        
        if value.get('item') == 'comment' and value.get('verb') == 'add':
            # Get the commenter's ID
            commenter_id = value.get('from', {}).get('id')
            
            # Check if this comment was made by our own page/app
            if self.is_own_comment(commenter_id, page_id):
                print(f"Detected our own comment from ID: {commenter_id}. Skipping processing.")
                return None  # Skip processing our own comments
            
            # Continue with regular comment processing
            page_access_token = self._get_stored_page_token(page_id)
            
            comment_data = {
                'comment_id': value.get('comment_id'),
                'post_id': value.get('post_id'),
                'parent_id': value.get('parent_id'),
                'message': value.get('message'),
                'created_time': value.get('created_time'),
                'from': {
                    'id': commenter_id,
                    'name': value.get('from', {}).get('name')
                }
            }
            
            # Check if it's a top-level comment by comparing parent_id with post_id
            is_top_level = value.get('parent_id') == value.get('post_id')
            
            # Get thread context and prepare for AI processing
            thread_context = self._get_comment_thread_context(
                value.get('post_id'),
                value.get('comment_id'),
                value.get('parent_id'),
                is_top_level,
                page_access_token
            )
            
            event_info.update({
                'page_access_token': page_access_token,
                'comment_data': comment_data,
                'thread_context': thread_context,
                'comment_level': 'top_level' if is_top_level else 'reply',
                'owner_info' : self.get_page_data(page_id,page_access_token),
                'post_data': {
                    'id': value.get('post', {}).get('id'),
                    'status_type': value.get('post', {}).get('status_type'),
                    'is_published': value.get('post', {}).get('is_published'),
                    'updated_time': value.get('post', {}).get('updated_time'),
                    'permalink_url': value.get('post', {}).get('permalink_url')
                }
            })
            
            print(f'EVENT_INFO: {event_info}')
            return event_info
        
        return None

    def _get_comment_thread_context(self, post_id, comment_id, parent_id, is_top_level, page_access_token):
        """
        Fetch the complete context of a comment thread
        
        :param post_id: ID of the post
        :param comment_id: ID of the current comment
        :param parent_id: ID of the parent comment
        :param is_top_level: Boolean indicating if this is a top-level comment
        :param page_access_token: Access token for the page
        :return: Dictionary containing thread context
        """
        base_url = "https://graph.facebook.com/v18.0"
        
        thread_context = {
            'post_content': None,
            'comment_thread': [],
            'hierarchy': 'top_level' if is_top_level else 'reply' #Probably always 'reply'
        }
        
        try:
            # 1. Get post content
            post_response = requests.get(
                f"{base_url}/{post_id}",
                params={
                    "fields": "message,created_time",
                    "access_token": page_access_token
                }
            )
            post_data = post_response.json()
            thread_context['post_content'] = post_data.get('message', '')
            
            # 2. Get comment thread
            if not is_top_level:
                # For replies, get the parent comment and its thread
                comment_response = requests.get(
                    f"{base_url}/{parent_id}",
                    params={
                        "fields": "message,created_time,from,comments{message,created_time,from}",
                        "access_token": page_access_token
                    }
                )
                thread_data = comment_response.json()
                print(f'THREAD DATA: {thread_data}')
                thread_context['comment_thread'].append({
                    'id': parent_id,
                    'message': thread_data.get('message'),
                    'created_time': thread_data.get('created_time'),
                    'from': thread_data.get('from'),
                    'replies': thread_data.get('comments', {}).get('data', [])
                })
                print(f'CONTEXT-POST: {thread_context['post_content']}')
                print(f'CONTEXT-COMMENT: {thread_context['comment_thread']}')
            else:
                # For top-level comments, get nearby comments for context
                comments_response = requests.get(
                    f"{base_url}/{post_id}/comments",
                    params={
                        "fields": "message,created_time,from,comments.limit(5){message,created_time,from}",
                        "access_token": page_access_token,
                        "limit": 5  # Adjust based on how much context you want
                    }
                )
                thread_context['comment_thread'] = comments_response.json().get('data', [])
        
        except requests.exceptions.RequestException as e:
            print(f"Error fetching thread context: {str(e)}")
            return thread_context

        return thread_context

    def extract_page_info(self, pages_data, page_id):
        """Extract 'category' and 'about' for a given page ID"""
        page_dict = {page["id"]: page for page in pages_data}  # Convert list to dict for fast lookup

        if page_id in page_dict:
            page = page_dict[page_id]
            page_access_token = page.get("access_token", "N/A")
            extended_page_access_token = self.extend_page_access_token(page_access_token)

            print(f"EXTENDED_TOKEN: {extended_page_access_token['access_token']}")
            try:
                self._store_page_token( page_id, extended_page_access_token['access_token'])

            except Exception as e:
                print(f"Error storing token: {str(e)}")  

            return {
                "name": page.get("name", "N/A"),
                "category": page.get("category", "N/A"),
                "about": page.get("about", "N/A"),
                "access_token" : page.get("access_token", "N/A"),
                "bio": page.get("bio", "N/A"),
                "description": page.get("description", "N/A"),
                "story" : page.get("story", "N/A"),
            }
        
        return {"error": "Page ID not found"}

    def _store_page_token(self, page_id, access_token):
        """
        Store page token in your database/cache
        Also fetches and stores the page's own ID for identity verification
        """
        try:            
            # Store token, page's ID, and timestamp
            table = boto3.resource('dynamodb').Table('facebook_page_tokens')
            table.put_item(Item={
                'page_id': page_id,
                'access_token': access_token,
                'updated_at': int(time.time())
            })
        except Exception as e:
            print(f"Error storing token: {str(e)}")      

    def _get_stored_page_token(self, page_id):
        """
        Get stored page token from your database/cache
        Implement based on your storage solution
        """
        # Example implementation using AWS DynamoDB
        try:
            table = boto3.resource('dynamodb').Table('facebook_page_tokens')
            response = table.get_item(Key={'page_id': page_id})
            print(f'RESPONSE: {response}')
            if 'Item' in response:
                return response['Item']['access_token']
        except Exception as e:
            print(f"Error getting stored token: {str(e)}")
        return None            

    def get_page_subscriptions(self, page_id, page_access_token):
        """
        Get all app subscriptions for a Facebook page
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :return: JSON response containing subscription information
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        params = {
            "access_token": page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            result = response.json()
            
            # Add logging for debugging
            print(f"Get page subscriptions response: {result}")
            
            # Format the response to make it more user-friendly
            subscriptions = []
            if 'data' in result:
                for app in result['data']:
                    subscriptions.append({
                        "app_id": app.get('id'),
                        "app_name": app.get('name', 'Unknown'),
                        "subscribed_fields": app.get('subscribed_fields', [])
                    })
            
            return {
                "status": "success",
                "page_id": page_id,
                "subscriptions": subscriptions,
                "raw_response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def subscribe_app_to_page(self, page_id, page_access_token, fields=None):
        """
        Subscribe the app to a Facebook page to receive real-time updates
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :param fields: Comma-separated string of fields to subscribe to (default: 'feed')
        :return: JSON response from the Facebook API
        """
        if fields is None:
            fields = 'feed'
            
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        params = {
            "access_token": page_access_token,
            "subscribed_fields": fields
        }
        
        try:
            response = requests.post(url, params=params)
            result = response.json()
            
            # Add some logging for debugging
            print(f"Subscribe app to page response: {result}")

            extended_page_access_token = self.extend_page_access_token(page_access_token)

            print(f"EXTENDED_TOKEN: {extended_page_access_token['access_token']}")

            self._store_page_token( page_id, extended_page_access_token['access_token'])
            
            return {
                "status": "success" if result.get('success') else "error",
                "page_id": page_id,
                "subscribed_fields": fields,
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id, 
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def unsubscribe_app_from_page_fields(self, page_id, page_access_token, fields_to_remove):
        """
        Unsubscribe the app from specific fields for a Facebook page
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :param fields_to_remove: String or list of fields to unsubscribe from
        :return: JSON response containing unsubscription result
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        # Convert string to list if necessary
        if isinstance(fields_to_remove, str):
            fields_to_remove = [fields_to_remove]
        
        try:
            # First, get current subscriptions
            current_subscriptions = self.get_page_subscriptions(page_id, page_access_token)
            
            if current_subscriptions["status"] == "error":
                return current_subscriptions
            
            # Get current subscribed fields from the first app (assuming it's the one we want)
            current_fields = []
            if current_subscriptions["subscriptions"]:
                current_fields = current_subscriptions["subscriptions"][0].get("subscribed_fields", [])
            
            # Remove specified fields while keeping others
            updated_fields = [field for field in current_fields if field not in fields_to_remove]
            
            if updated_fields:
                # If there are still fields left, update the subscription
                params = {
                    "access_token": page_access_token,
                    "subscribed_fields": ','.join(updated_fields)
                }
                response = requests.post(url, params=params)
            else:
                # If no fields are left, unsubscribe the app completely
                params = {"access_token": page_access_token}
                response = requests.delete(url, params=params)  # DELETE request unsubscribes the app

            result = response.json()
            
            # Add logging for debugging
            print(f"Unsubscribe fields response: {result}")
            
            return {
                "status": "success" if result.get('success') else "error",
                "page_id": page_id,
                "removed_fields": fields_to_remove,
                "remaining_fields": updated_fields,
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def get_instagram_profile_details(self, instagram_id, page_access_token):
        """
        Retrieve detailed Instagram profile information including biography, username, profile picture, and website
        
        :param instagram_id: The Instagram Business Account ID
        :param page_access_token: Access token for the connected Facebook page
        :return: Dictionary containing Instagram profile details
        """
        try:
            url = f"https://graph.facebook.com/v18.0/{instagram_id}"
            params = {
                "fields": "biography,username,profile_picture_url,website,followers_count,follows_count,media_count,name,ig_id",
                "access_token": page_access_token
            }
            
            response = requests.get(url, params=params)
            result = response.json()
            
            if 'error' in result:
                print(f"Error fetching Instagram profile: {result['error']}")
                return {
                    "status": "error",
                    "error_details": result['error'],
                    "timestamp": datetime.now().isoformat()
                }
            
            # Return the Instagram profile data
            return {
                "status": "success",
                "instagram_id": result.get('id'),
                "ig_id": result.get('ig_id'),  # This is the actual Instagram user ID
                "username": result.get('username'),
                "name": result.get('name'),
                "biography": result.get('biography'),
                "website": result.get('website'),
                "profile_picture_url": result.get('profile_picture_url'),
                "followers_count": result.get('followers_count'),
                "follows_count": result.get('follows_count'),
                "media_count": result.get('media_count'),
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            print(f"Exception while fetching Instagram profile: {str(e)}")
            return {
                "status": "error",
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def post_to_instagram(self, instagram_id, page_access_token, caption, image_url):
        """
        Publish a text/image post to a linked Instagram Business account.
        
        :param instagram_id: The Instagram Business account ID (from get_facebook_pages)
        :param page_access_token: The access token of the connected Facebook Page
        :param caption: The caption/text of the post
        :param image_url: Publicly accessible image URL
        :return: JSON response from the Instagram Graph API
        """
        try:
            # Step 1: Create media container
            create_url = f"https://graph.facebook.com/v18.0/{instagram_id}/media"
            create_params = {
                "image_url": image_url,
                "caption": caption,
                "access_token": page_access_token
            }
            create_resp = requests.post(create_url, data=create_params).json()
            
            if "id" not in create_resp:
                return {"status": "error", "step": "media", "response": create_resp}
            
            creation_id = create_resp["id"]

            # Step 2: Publish media
            publish_url = f"https://graph.facebook.com/v18.0/{instagram_id}/media_publish"
            publish_params = {
                "creation_id": creation_id,
                "access_token": page_access_token
            }
            publish_resp = requests.post(publish_url, data=publish_params).json()

            return {
                "status": "success" if "id" in publish_resp else "error",
                "creation_id": creation_id,
                "publish_response": publish_resp
            }
        except Exception as e:
            return {"status": "error", "details": str(e)}
