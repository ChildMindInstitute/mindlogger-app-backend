# -*- coding: utf-8 -*-
import copy
import datetime
import json
import os
import six

from bson.objectid import ObjectId
from girderformindlogger import events
from girderformindlogger.constants import AccessType
from girderformindlogger.exceptions import ValidationException, GirderException
from girderformindlogger.models.model_base import AccessControlledModel, Model
from girderformindlogger.models.push_notification import PushNotification as PushNotificationModel
from girderformindlogger.models.profile import Profile
from girderformindlogger.utility.model_importer import ModelImporter
from girderformindlogger.utility.progress import noProgress, setResponseTimeLimit
from bson import json_util


class Events(Model):
    """
    collection for manage schedule and notification.
    """

    def initialize(self):
        self.name = 'events'
        self.ensureIndices(
            (
                'applet_id',
                'individualized',
                'data.users'
            )
        )

    def validate(self, document):
        return document

    def deleteEvent(self, event_id):
        event = self.findOne({'_id': ObjectId(event_id)})
        if event:
            push_notification = PushNotificationModel(event=event)
            push_notification.remove_schedules()
        self.removeWithQuery({'_id': ObjectId(event_id)})

    def upsertEvent(self, event, applet_id, event_id=None):
        newEvent = {
            'applet_id': applet_id,
            'individualized': False,
            'schedulers': [],
            'sendTime': [],
            'data': {}
        }
        existed_event = self.findOne({'_id': ObjectId(event_id)}, fields=['_id', 'schedulers', 'data'])

        if event_id and existed_event:
            newEvent['_id'] = ObjectId(event_id)
            newEvent['schedulers'] = existed_event.get('schedulers', [])

        if 'data' in event:
            newEvent['data'] = event['data']
            if 'users' in event['data'] and isinstance(event['data']['users'], list):
                newEvent['individualized'] = True
                event['data']['users'] = [ObjectId(profile_id) for profile_id in event['data']['users']]

                self.updateIndividualSchedulesParameter(newEvent, existed_event)

        if 'schedule' in event:
            newEvent['schedule'] = event['schedule']

        newEvent = self.save(newEvent)
        self.setSchedule(newEvent)

        return self.save(newEvent)

    def updateIndividualSchedulesParameter(self, newEvent, oldEvent):
        new = newEvent['data']['users'] if 'users' in newEvent['data'] else []
        old = newEvent['data']['users'] if oldEvent else []

        dicrementedUsers = list(set(old).difference(set(new)))
        incrementedUsers = list(set(new).difference(set(old)))

        if len(dicrementedUsers):
            Profile().update(query={
                "_id": {
                    "$in": dicrementedUsers
                }
            }, update={'$inc': {
                    'individual_events': -1
                }
            })

        if len(incrementedUsers):
            Profile().update(query={
                "_id": {
                    "$in": incrementedUsers
                }
            }, update={'$inc': {
                    'individual_events': 1
                }
            })

    def rescheduleRandomNotifications(self, event):
        if 'data' in event and 'useNotifications' in event['data'] and event['data'][
            'useNotifications']:
            push_notification = PushNotificationModel(event=event)
            push_notification.random_reschedule()
            self.save(event)

    def hasIndividual(self, applet_id, profileId):
        return (self.findOne({'applet_id': ObjectId(applet_id), 'data.users': profileId}) is not None)

    def getEvents(self, applet_id, individualized):
        events = list(self.find({'applet_id': ObjectId(applet_id), 'individualized': individualized}, fields=['data', 'schedule']))
        for event in events:
            if 'data' in event and 'users' in event['data']:
                event['data'].pop('users')

        return events

    def setSchedule(self, event):
        if 'data' in event and 'useNotifications' in event['data'] and event['data'][
            'useNotifications']:
            if 'notifications' in event['data'] and event['data']['notifications'][0]['start']:
                push_notification = PushNotificationModel(event=event)
                push_notification.set_schedules()

    def cancelSchedules(self, event):
        pass
        # if 'schedulers' in event and len(event['schedulers']):


    def getSchedule(self, applet_id):
        events = list(self.find({'applet_id': ObjectId(applet_id)}, fields=['data', 'schedule']))

        for event in events:
            event['id'] = event['_id']
            event.pop('_id')

        return {
            "type": 2,
            "size": 1,
            "fill": True,
            "minimumSize": 0,
            "repeatCovers": True,
            "listTimes": False,
            "eventsOutside": True,
            "updateRows": True,
            "updateColumns": False,
            "around": 1585724400000,
            'events': events
        }

    def getScheduleForUser(self, applet_id, user_id, is_coordinator):
        if is_coordinator:
            individualized = False
        else:
            profile = Profile().findOne({'appletId': ObjectId(applet_id), 'userId': ObjectId(user_id)})
            individualized = self.hasIndividual(applet_id, profile['_id'])

        events = self.getEvents(applet_id, individualized)
        for event in events:
            event['id'] = event['_id']
            event.pop('_id')

        return {
            "type": 2,
            "size": 1,
            "fill": True,
            "minimumSize": 0,
            "repeatCovers": True,
            "listTimes": False,
            "eventsOutside": True,
            "updateRows": True,
            "updateColumns": False,
            "around": 1585724400000,
            'events': events
        }
