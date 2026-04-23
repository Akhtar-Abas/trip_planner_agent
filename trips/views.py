from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json


def chat_page(request):
    """ WebSocket """
    return render(request, 'trips/chat.html')


@csrf_exempt
def api_start(request):
    if request.method == 'POST':
        from .services import start_conversation
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key
        result = start_conversation(session_key)
        return JsonResponse(result)
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def api_send(request):
    if request.method == 'POST':
        from .services import process_user_message
        data = json.loads(request.body)
        thread_id = data.get('thread_id')
        message = data.get('message')
        if not thread_id or not message:
            return JsonResponse({'error': 'Missing thread_id or message'}, status=400)
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key
        result = process_user_message(thread_id, message, session_key)
        return JsonResponse(result)
    return JsonResponse({'error': 'Method not allowed'}, status=405)