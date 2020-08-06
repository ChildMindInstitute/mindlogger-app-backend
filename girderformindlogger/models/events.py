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
from girderformindlogger.models.folder import Folder
from girderformindlogger.utility.model_importer import ModelImporter
from girderformindlogger.utility.progress import noProgress, setResponseTimeLimit
from bson import json_util
from girderformindlogger.models.profile import Profile as ProfileModel


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
            if event['individualized']:
                ProfileModel().update(query={
                    "_id": {
                        "$in": event['data']['users']
                    }
                    }, update={'$inc': {
                        'individual_events': -1
                    }
                })

            push_notification = PushNotificationModel(event=event)
            push_notification.remove_schedules()
            self.removeWithQuery({'_id': ObjectId(event_id)})

    def deleteEventsByAppletId(self, applet_id):
        events = self.find({'applet_id': ObjectId(applet_id)})

        for event in events:
            self.deleteEvent(event.get('_id'))

    def upsertEvent(self, event, applet, event_id=None):
        newEvent = {
            'applet_id': applet['_id'],
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

            activities = list(Folder().find(query={
                'meta.activity.url': event['data']['URI']
            }, fields=['_id']))

            activity_id = list(set(applet["meta"]["protocol"]["activities"]) & set(
                [activity['_id'] for activity in activities]))

            if len(activity_id):
                newEvent['data']['activity_id'] = activity_id[0]

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


    def getEvents(self, applet_id, individualized, profile_id = None):
        if not individualized or not profile_id:
            events = list(self.find({'applet_id': ObjectId(applet_id), 'individualized': individualized}, fields=['data', 'schedule']))
        else:
            events = list(self.find({'applet_id': ObjectId(applet_id), 'individualized': individualized, 'data.users': profile_id}, fields=['data', 'schedule']))

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

    def dateMatch(self, event, date): # filter only active events on specified date
        eventTimeout = event['data'].get('timeout', None)
        eventTime = event['schedule']['times'][0] if 'times' in event['schedule'] else '00:00'
        if ':' not in eventTime:
            eventTime = f'{eventTime}:00'

        timeDelta = datetime.timedelta(hours=int(eventTime[:2]), minutes=int(eventTime[-2:]))

        timeout = datetime.timedelta(days=0)

        if eventTimeout and eventTimeout.get('allow', False):
            timeout = datetime.timedelta(
                days=eventTimeout.get('day', 0), 
                hours=eventTimeout.get('hour', 0), 
                minutes=eventTimeout.get('minute', 0)
            )

        if 'dayOfMonth' in event['schedule']: # one time schedule
            if not len(event['schedule'].get('dayOfMonth', [])) \
                or not len(event['schedule'].get('month', [])) \
                or not len(event['schedule'].get('year', [])):

                return False

            launchDate = datetime.datetime.strptime(
                f'{event["schedule"]["year"][0]}/{event["schedule"]["month"][0]+1}/{event["schedule"]["dayOfMonth"][0]}',
                '%Y/%m/%d'
            ) + timeDelta

            return launchDate.date() == date.date() or (launchDate + timeout).date() == date.date()

        else:
            start = event['schedule'].get('start', None)
            end = event['schedule'].get('end', None)

            startDate = datetime.datetime.fromtimestamp(start/1000) + timeDelta if start else None
            endDate = datetime.datetime.fromtimestamp(end/1000) + timeDelta if end else None

            if startDate and startDate.date() > date.date():
                return False

            if 'dayOfWeek' in event['schedule']: # weekly schedule
                if len(event['schedule']['dayOfWeek']) or event['schedule']['dayOfWeek'][0] == date.weekday() + 1:
                    return True

                if endDate < date:
                    latestScheduledDay = endDate - datetime.timedelta(
                        days=(endDate.weekday()+1 - event['schedule']['dayOfWeek'][0] + 7) % 7,
                    )
                else:
                    latestScheduledDay = date - datetime.timedelta(
                        days=(date.weekday()+1 - event['schedule']['dayOfWeek'][0] + 7) % 7
                    )

                return (not startDate or startDate.date() <= latestScheduledDay.date()) \
                        and latestScheduledDay + timeDelta + timeout >= date

            # daily schedule
            return (not endDate or endDate + timeDelta + timeout >= date)

    def getScheduleForUser(self, applet_id, user_id, is_coordinator, dayFilter=None):
        if is_coordinator:
            individualized = False
            events = self.getEvents(applet_id, False)
        else:
            profile = Profile().findOne({'appletId': ObjectId(applet_id), 'userId': ObjectId(user_id)})
            individualized = profile['individual_events'] > 0
            events = self.getEvents(applet_id, individualized, profile['_id'])

        for event in events:
            event['id'] = event['_id']
            event.pop('_id')

            event['invalid'] = False
            if dayFilter:
                event['invalid'] = not self.dateMatch(event, dayFilter)

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
            'events': [
                event for event in events if not event['invalid']
            ]
        }
