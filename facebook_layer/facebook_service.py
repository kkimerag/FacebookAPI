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

    def init_reel_upload(self, page_id, page_access_token, description, video_url, platform="facebook", instagram_id=None):
        """
        Step 1: Initialize a reel/video upload for Facebook or Instagram
        
        :param page_id: The Facebook page ID (for Facebook) or ignored (for Instagram)
        :param page_access_token: The access token for the page
        :param description: The description/caption for the reel/video
        :param video_url: The URL of the video to use
        :param platform: "facebook" or "instagram"
        :param instagram_id: Required for Instagram platform
        :return: Dictionary with upload session details
        """
        try:
            if platform.lower() == "instagram":
                instagram_id = page_id
                if not instagram_id:
                    return {
                        "status": "error",
                        "platform": platform,
                        "error_details": "instagram_id is required for Instagram platform",
                        "phase": "initialization",
                        "timestamp": datetime.now().isoformat()
                    }
                
                # Instagram: Create media container
                create_url = f"https://graph.facebook.com/v22.0/{instagram_id}/media"
                create_params = {
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": description,
                    "access_token": page_access_token,
                    "share_to_feed": "true"
                }
                
                create_resp = requests.post(create_url, data=create_params).json()
                
                if "id" not in create_resp:
                    return {
                        "status": "error",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "error_details": create_resp,
                        "phase": "media_creation",
                        "timestamp": datetime.now().isoformat()
                    }
                
                return {
                    "status": "pending",
                    "platform": platform,
                    "instagram_id": instagram_id,
                    "creation_id": create_resp["id"],  # This is the container ID for Instagram
                    "video_id": create_resp["id"],   #Expected for the next State
                    "description": description,
                    "phase": "initialized",
                    "timestamp": datetime.now().isoformat()
                }
                
            else:  # Facebook
                # Original Facebook implementation
                start_url = f"https://graph.facebook.com/v22.0/{page_id}/video_reels"
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
                        "platform": platform,
                        "page_id": page_id,
                        "error_details": start_result['error'],
                        "phase": "start",
                        "timestamp": datetime.now().isoformat()
                    }
                
                video_id = start_result.get('video_id')
                if not video_id:
                    return {
                        "status": "error",
                        "platform": platform,
                        "page_id": page_id,
                        "error_details": "Missing video_id in start response",
                        "phase": "start",
                        "timestamp": datetime.now().isoformat()
                    }
                
                return {
                    "status": "pending",
                    "platform": platform,
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
                "platform": platform,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
                "phase": "initialization",
                "timestamp": datetime.now().isoformat()
            }

    def upload_hosted_file(self, page_id, page_access_token, video_id, file_url, platform="facebook", **kwargs):
        """
        Upload a hosted video file - Facebook only step, Instagram skips this
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param video_id: The ID of the video from the init step
        :param file_url: The URL of the hosted video file
        :param platform: "facebook" or "instagram"
        :return: Dictionary with upload status
        """
        try:
            if platform.lower() == "instagram":
                # Instagram doesn't need this step - video is already being processed from init step
                return {
                    "status": "success",
                    "platform": platform,
                    "phase": "upload_skipped_for_instagram",
                    "message": "Instagram processes video directly from URL in init step",
                    "timestamp": datetime.now().isoformat()
                }
            
            else:  # Facebook - original implementation
                print(f"[DEBUG] Starting hosted file upload for video_id: {video_id}, page_id: {page_id}")
                print(f"[DEBUG] File URL: {file_url}")
                
                # Validate file_url
                if not file_url.startswith('https://'):
                    return {
                        "status": "error",
                        "platform": platform,
                        "page_id": page_id,
                        "video_id": video_id,
                        "error_details": "File URL must use HTTPS protocol",
                        "phase": "upload_hosted_file",
                        "timestamp": datetime.now().isoformat()
                    }
                    
                # Check if the host is not a Meta CDN
                parsed_url = urlparse(file_url)
                if 'fbcdn.net' in parsed_url.netloc.lower():
                    return {
                        "status": "error",
                        "platform": platform,
                        "page_id": page_id,
                        "video_id": video_id,
                        "error_details": "Files hosted on Meta CDN (fbcdn) are not supported. Use crossposting instead.",
                        "phase": "upload_hosted_file",
                        "timestamp": datetime.now().isoformat()
                    }
                    
                upload_url = f"https://rupload.facebook.com/video-upload/v22.0/{video_id}"
                headers = {
                    "Authorization": f"OAuth {page_access_token}",
                    "file_url": file_url
                }
                
                response = requests.post(upload_url, headers=headers)
                result = response.json()
                
                if result.get('success') is True:
                    return {
                        "status": "success",
                        "platform": platform,
                        "page_id": page_id,
                        "video_id": video_id,
                        "phase": "file_uploaded",
                        "timestamp": datetime.now().isoformat()
                    }
                else:
                    return {
                        "status": "error",
                        "platform": platform,
                        "page_id": page_id,
                        "video_id": video_id,
                        "error_details": result.get('error', 'Unknown error'),
                        "phase": "upload_hosted_file",
                        "timestamp": datetime.now().isoformat()
                    }
                    
        except Exception as e:
            import traceback
            return {
                "status": "error",
                "platform": platform,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
                "phase": "upload_hosted_file",
                "timestamp": datetime.now().isoformat()
            }
 
    def check_reel_upload_status(self, page_id, page_access_token, video_id, platform="facebook", instagram_id=None, creation_id=None):
        """
        Check if the uploaded video is ready for both platforms
        
        :param page_id: The Facebook page ID (for Facebook)
        :param page_access_token: The access token for the page
        :param video_id: The ID of the video being processed (Facebook)
        :param platform: "facebook" or "instagram"
        :param instagram_id: Instagram Business Account ID (for Instagram)
        :param creation_id: Container ID for Instagram
        :return: Dictionary with upload status
        """
        try:
            if platform.lower() == "instagram":
                instagram_id = page_id
                creation_id = video_id
                if not creation_id:
                    return {
                        "status": "error",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "error_details": "creation_id is required for Instagram status check",
                        "phase": "check_status",
                        "timestamp": datetime.now().isoformat()
                    }
                
                # Check Instagram container status
                status_url = f"https://graph.facebook.com/v22.0/{creation_id}"
                status_params = {
                    "fields": "status_code",
                    "access_token": page_access_token
                }
                status_response = requests.get(status_url, params=status_params)
                status_result = status_response.json()
                
                if 'error' in status_result:
                    return {
                        "status": "error",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "creation_id": creation_id,
                        "error_details": status_result['error'],
                        "phase": "check_status",
                        "timestamp": datetime.now().isoformat()
                    }
                
                if 'status_code' in status_result:
                    status_code = status_result['status_code']
                    
                    if status_code == 'FINISHED':
                        return {
                            "status": "ready",
                            "platform": platform,
                            "instagram_id": instagram_id,
                            "creation_id": creation_id,
                            "phase": "video_ready",
                            "timestamp": datetime.now().isoformat()
                        }
                    elif status_code == 'ERROR':
                        return {
                            "status": "error",
                            "platform": platform,
                            "instagram_id": instagram_id,
                            "creation_id": creation_id,
                            "error_details": "Video processing failed",
                            "phase": "processing",
                            "timestamp": datetime.now().isoformat()
                        }
                    else:
                        # Still processing
                        return {
                            "status": "processing",
                            "platform": platform,
                            "instagram_id": instagram_id,
                            "creation_id": creation_id,
                            "status_code": status_code,
                            "phase": "awaiting_ready",
                            "timestamp": datetime.now().isoformat()
                        }
                else:
                    return {
                        "status": "unknown",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "creation_id": creation_id,
                        "raw_response": status_result,
                        "phase": "check_status",
                        "timestamp": datetime.now().isoformat()
                    }
                    
            else:  # Facebook - original implementation
                status_url = f"https://graph.facebook.com/v22.0/{video_id}"
                status_params = {
                    "fields": "status",
                    "access_token": page_access_token
                }
                status_response = requests.get(status_url, params=status_params)
                status_result = status_response.json()
                
                if 'error' in status_result:
                    return {
                        "status": "error",
                        "platform": platform,
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
                            "platform": platform,
                            "page_id": page_id,
                            "video_id": video_id,
                            "phase": "video_ready",
                            "timestamp": datetime.now().isoformat()
                        }
                    elif video_status == 'error':
                        return {
                            "status": "error",
                            "platform": platform,
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
                            "platform": platform,
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
                        "platform": platform,
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
                "platform": platform,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
                "phase": "check_status",
                "timestamp": datetime.now().isoformat()
            }

    def publish_reel(self, page_id, page_access_token, video_id, description, platform="facebook", share_to_feed=True, audio_name=None, thumbnail_url=None, instagram_id=None, creation_id=None, **kwargs):

        """
        Publish the reel/video once ready for both platforms
        
        :param page_id: The Facebook page ID (for Facebook)
        :param page_access_token: The access token for the page
        :param video_id: The ID of the processed video (Facebook)
        :param description: The description/caption for the content
        :param platform: "facebook" or "instagram"
        :param instagram_id: Instagram Business Account ID (for Instagram)
        :param creation_id: Container ID for Instagram
        :param share_to_feed: Whether to share to main feed
        :return: Dictionary with publish status
        """

        print(f'PLATFORM: {platform}')

        try:
            if platform.lower() == "instagram":

                instagram_id = page_id
                creation_id = video_id

                if not instagram_id or not creation_id:
                    return {
                        "status": "error",
                        "platform": platform,
                        "error_details": "instagram_id and creation_id are required for Instagram publishing",
                        "phase": "publish",
                        "timestamp": datetime.now().isoformat()
                    }
                
                # Publish Instagram container
                publish_url = f"https://graph.facebook.com/v22.0/{instagram_id}/media_publish"
                publish_params = {
                    "creation_id": creation_id,
                    "access_token": page_access_token
                }
                
                publish_resp = requests.post(publish_url, data=publish_params).json()
                
                if "id" in publish_resp:
                    return {
                        "status": "success",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "media_id": publish_resp["id"],
                        "creation_id": creation_id,
                        "phase": "published",
                        "timestamp": datetime.now().isoformat()
                    }
                else:
                    return {
                        "status": "error",
                        "platform": platform,
                        "instagram_id": instagram_id,
                        "creation_id": creation_id,
                        "error_details": publish_resp,
                        "phase": "publish",
                        "timestamp": datetime.now().isoformat()
                    }
                    
            else:  # Facebook - original implementation
                finish_url = f"https://graph.facebook.com/v22.0/{page_id}/video_reels"
                
                finish_params = {
                    "upload_phase": "finish",
                    "video_id": video_id,
                    "description": description,
                    "share_to_feed": "true" if share_to_feed else "false",
                    "access_token": page_access_token,
                    "video_state": "PUBLISHED"
                }
                
                # Add optional parameters
                if kwargs.get('audio_name'):
                    finish_params["audio_name"] = kwargs['audio_name']
                if kwargs.get('thumbnail_url'):
                    finish_params["thumbnail_url"] = kwargs['thumbnail_url']
                
                finish_response = requests.post(finish_url, data=finish_params)
                finish_result = finish_response.json()
                
                # Check for success
                if 'success' in finish_result and finish_result['success'] is True:
                    post_id = finish_result.get('post_id', None)
                    
                    return {
                        "status": "success",
                        "platform": platform,
                        "page_id": page_id,
                        "reel_id": post_id,
                        "video_id": video_id,
                        "message": finish_result.get('message'),
                        "share_to_feed": share_to_feed,
                        "phase": "published",
                        "timestamp": datetime.now().isoformat()
                    }
                elif 'id' in finish_result:
                    return {
                        "status": "success",
                        "platform": platform,
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
                    
                    return {
                        "status": "error",
                        "platform": platform,
                        "page_id": page_id,
                        "video_id": video_id,
                        "error_details": error_details,
                        "phase": "publish",
                        "timestamp": datetime.now().isoformat()
                    }
                    
        except Exception as e:
            import traceback
            return {
                "status": "error",
                "platform": platform,
                "error_details": str(e),
                "traceback": traceback.format_exc(),
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

    def post_to_instagram(self, instagram_id, page_access_token, caption, mediaType, mm_url=None):
        """Debug version with detailed logging"""
        try:
            # Test video URL accessibility first
            if mediaType == "video" and mm_url:
                print(f"Testing video URL: {mm_url}")
                test_resp = requests.head(mm_url)
                print(f"Video URL status: {test_resp.status_code}")
                print(f"Content-Type: {test_resp.headers.get('content-type')}")
                print(f"Content-Length: {test_resp.headers.get('content-length')}")
            
            create_url = f"https://graph.facebook.com/v19.0/{instagram_id}/media"  # Updated API version
            
            if mediaType == "video":
                if not mm_url:
                    return {"status": "error", "details": "Missing video URL"}
                create_params = {
                    "media_type": "REELS",
                    "video_url": mm_url,
                    "caption": caption,
                    "access_token": page_access_token,
                    "share_to_feed": "TRUE"
                }
            elif mediaType == "image":
                if not mm_url:
                    return {"status": "error", "details": "Missing image URL"}
                create_params = {
                    "image_url": mm_url,
                    "caption": caption,
                    "access_token": page_access_token
                }
            else:  # text-only
                if not caption:
                    return {"status": "error", "details": "Missing caption for text-only post"}
                create_params = {
                    "caption": caption,
                    "access_token": page_access_token
                }
            
            print(f"Creating media with params: {create_params}")
            create_resp = requests.post(create_url, data=create_params)
            
            print(f"Create response status: {create_resp.status_code}")
            print(f"Create response headers: {dict(create_resp.headers)}")
            
            create_json = create_resp.json()
            print(f"Create response JSON: {create_json}")
            
            if "id" not in create_json:
                return {"status": "error", "step": "media", "response": create_json}
            
            creation_id = create_json["id"]
            print(f"Creation ID: {creation_id}")
            
            # For videos, check status
            if mediaType == "video":
                status_url = f"https://graph.facebook.com/v19.0/{creation_id}"
                status_params = {
                    "fields": "status_code",
                    "access_token": page_access_token
                }
                
                for i in range(5):  # Check 5 times
                    time.sleep(5)
                    status_resp = requests.get(status_url, params=status_params).json()
                    print(f"Status check {i+1}: {status_resp}")
                    
                    if status_resp.get("status_code") == "FINISHED":
                        break
                    elif status_resp.get("status_code") == "ERROR":
                        return {"status": "error", "step": "processing", "response": status_resp}
            
            # Publish
            publish_url = f"https://graph.facebook.com/v19.0/{instagram_id}/media_publish"
            publish_params = {
                "creation_id": creation_id,
                "access_token": page_access_token
            }
            
            print(f"Publishing with params: {publish_params}")
            publish_resp = requests.post(publish_url, data=publish_params)
            
            print(f"Publish response status: {publish_resp.status_code}")
            publish_json = publish_resp.json()
            print(f"Publish response JSON: {publish_json}")
            
            return {
                "status": "success" if "id" in publish_json else "error",
                "creation_id": creation_id,
                "publish_response": publish_json
            }
            
        except Exception as e:
            print(f"Exception occurred: {str(e)}")
            return {"status": "error", "details": str(e)}