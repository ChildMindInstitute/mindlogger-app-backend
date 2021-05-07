from bson import json_util
from copy import deepcopy
from datetime import datetime
from girderformindlogger.constants import AccessType, PREFERRED_NAMES, DEFINED_RELATIONS,       \
    HIERARCHY, KEYS_TO_DELANGUAGETAG, KEYS_TO_DEREFERENCE, KEYS_TO_EXPAND,     \
    MODELS, NONES, REPROLIB_CANONICAL, REPROLIB_PREFIXES
from girderformindlogger.exceptions import AccessException,                    \
    ResourcePathNotFound, ValidationException
from girderformindlogger.models.activity import Activity as ActivityModel
from girderformindlogger.models.applet import Applet as AppletModel
from girderformindlogger.models.collection import Collection as CollectionModel
from girderformindlogger.models.folder import Folder as FolderModel
from girderformindlogger.models.item import Item as ItemModel
from girderformindlogger.models.protocol import Protocol as ProtocolModel
from girderformindlogger.models.screen import Screen as ScreenModel
from girderformindlogger.models.user import User as UserModel
from girderformindlogger.utility import loadJSON
from girderformindlogger.utility.response import responseDateList
from girderformindlogger.models.cache import Cache as CacheModel
from bson.objectid import ObjectId
from pyld import jsonld
from pymongo import ASCENDING, DESCENDING


def getModelCollection(modelType):
    """
    Returns the Collection named for the given modelType, creating if not
    already extant.

    :param modelType: 'activity', 'screen', etc.
    :type modelType: str
    :returns: dict
    """
    from girderformindlogger.models import pluralize
    name = pluralize(modelType).title()
    collection = CollectionModel().findOne(
        {'name': name}
    )
    if not collection:
        collection = CollectionModel().createCollection(
            name=name,
            public=True,
            reuseExisting=True
        )
    return(collection)


def expandObj(contextSet, data):
    obj = deepcopy(data)
    context = {}

    if '@context' in data:
        for key in data['@context']:
            context.update(deepcopy(contextSet[key]))

    if len(context.keys()):
        obj['@context'] = context

    expanded = expand(obj)
    if '@context' in expanded:
        expanded.pop('@context')

    if '@id' not in expanded:
        raise ValidationException('unable to load id')

    return expanded

def convertObjectToSingleFileFormat(obj, modelType, user, identifier=None, refreshCache=False):
    modelClass = MODELS()[modelType]()

    model = obj.get('meta', {}).get(modelType, None)
    if model:
        for key in ['url', 'schema:url']:
            if key in model:
                model.pop(key)

    obj.update({
        'loadedFromSingleFile': True,
        'lastUpdatedBy': user['_id']
    })

    if identifier:
        obj['meta']['identifier'] = identifier

    modelClass.setMetadata(obj, obj.get('meta', {}))

    if refreshCache:
        clearCache(obj, modelType)

        formatted = formatLdObject(
            obj,
            mesoPrefix=modelType,
            user=user,
            refreshCache=True
        )

        createCache(obj, formatted, modelType, user)

# insert historical data in the database
def insertHistoryData(obj, identifier, modelType, baseVersion, historyFolder, historyReferenceFolder, user):
    if modelType not in ['activity', 'screen']:
        return

    modelClass = MODELS()[modelType]()

    # insert historical data
    if obj:
        if '_id' in obj:
            obj['meta']['originalId'] = obj.pop('_id')
        if 'cached' in obj:
            obj.pop('cached')

        if 'protocolId' in obj['meta']:
            obj['meta'].pop('protocolId')

        obj['meta']['historyId'] = historyFolder['_id']
        obj['meta']['version'] = baseVersion
        if modelClass.name == 'folder':
            obj['parentId'] = historyFolder['_id']
        elif modelClass.name == 'item':
            obj['folderId'] = historyFolder['_id']

        meta = obj.pop('meta')

        if modelClass.name == 'folder':
            obj = FolderModel().createFolder(
                name=obj['name'],
                parent=historyFolder,
                parentType='folder',
                public=True,
                creator=user,
                allowRename=True,
                reuseExisting=False
            )
        else:
            obj = modelClass.createItem(
                name=obj['name'],
                creator=user,
                folder=historyFolder,
                reuseExisting=False
            )

        obj = modelClass.setMetadata(obj, meta)
        formatted = _fixUpFormat(formatLdObject(
            obj,
            mesoPrefix=modelType,
            user=user,
            refreshCache=True
        ))

        if modelType == 'screen':
            formatted['original'] = {
                'screenId': obj['meta'].get('originalId', None),
                'activityId': obj['meta'].get('originalActivityId', None)
            }

            formatted['activityId'] = obj['meta'].get('activityId', None)
        else:
            formatted['original'] = {
                'activityId': obj['meta'].get('originalId', None)
            }

        obj = createCache(obj, formatted, modelClass.name, user)

    itemModel = ItemModel()

    # update references
    referenceObj = itemModel.findOne({
        'folderId': historyReferenceFolder['_id'],
        'meta.identifier': identifier
    })
    if not referenceObj:
        referenceObj = itemModel.setMetadata(itemModel.createItem(
            name='history of {}'.format(identifier),
            creator=user,
            folder=historyReferenceFolder,
            reuseExisting=False
        ), {
            'identifier': identifier,
            'modelType': modelType,
            'history': []
        })

    now = datetime.utcnow()

    itemModel.update({'_id': referenceObj['_id']}, {
        '$push': {
            'meta.history': {
                'version': baseVersion,
                'reference': '{}/{}'.format(modelType, str(obj['_id'])) if obj else None,
                'updated': now
            }
        },
        '$set': {
            'updated': now,
            'meta.lastVersion': baseVersion
        }
    })

    return obj

def createProtocolFromExpandedDocument(protocol, user, editExisting=False, removed={}, baseVersion=None):
    protocolId = None
    historyFolder = None
    historyReferenceFolder = None

    for modelType in ['protocol', 'activity', 'screen']:
        modelClass = MODELS()[modelType]()
        docCollection = getModelCollection(modelType)

        for model in protocol[modelType].values():
            prefName = modelClass.preferredName(model['expanded'])

            if modelClass.name in ['folder', 'item']:
                docFolder = None
                item = None

                metadata = {modelType: model['expanded']}

                tmp = model
                while tmp.get('parentId', None):
                    key = tmp['parentKey']
                    tmp = protocol[key][tmp['parentId']]

                    metadata['{}Id'.format(key)] = tmp['_id']

                if model['ref2Document'].get('_id', None) and editExisting:
                    # in case of edit existing item/activity
                    if modelClass.name == 'folder':
                        try:
                            docFolder = FolderModel().load(model['ref2Document']['_id'], force=True)

                            if 'identifier' in docFolder['meta'] and modelType == 'activity':
                                model['historyObj'] = insertHistoryData(deepcopy(docFolder), docFolder['meta']['identifier'], modelType, baseVersion, historyFolder, historyReferenceFolder, user)

                            docFolder['name'] = prefName

                            FolderModel().updateFolder(docFolder)
                        except Exception as e:
                            print('wrong folder id', model['ref2Document']['_id'])
                            print(e)
                    elif modelClass.name == 'item':
                        try:
                            item = modelClass.load(model['ref2Document']['_id'],force=True)

                            if 'identifier' in item['meta']:
                                clonedItem = deepcopy(item)
                                if 'activityId' in clonedItem['meta']:
                                    clonedItem['meta'].update({
                                        'originalActivityId': clonedItem['meta']['activityId'],
                                        'activityId': protocol[model['parentKey']][model['parentId']]['historyObj']['_id']
                                    })

                                insertHistoryData(clonedItem, item['meta']['identifier'], modelType, baseVersion, historyFolder, historyReferenceFolder, user)

                            docFolder = FolderModel().findOne({'_id': item['folderId']})

                            if docFolder:
                                docFolder['name'] = prefName
                                FolderModel().updateFolder(docFolder)
                        except Exception as e:
                            print('wrong item id', model['ref2Document']['_id'])
                            print(e)


                if not docFolder:
                    docFolder = FolderModel().createFolder(
                        name=prefName,
                        parent=docCollection,
                        parentType='collection',
                        public=True,
                        creator=user,
                        allowRename=True,
                        reuseExisting=(modelClass.name == 'item')
                    )

                    if modelType == 'activity':
                        metadata['identifier'] = docFolder['_id']

                        if editExisting:
                            insertHistoryData(None, metadata['identifier'], modelType, baseVersion, historyFolder, historyReferenceFolder, user)

                if modelClass.name=='folder':
                    newModel = modelClass.setMetadata(
                        docFolder,
                        {
                            **docFolder.get('meta', {}),
                            **metadata
                        }
                    )

                elif modelClass.name=='item':
                    name = prefName if prefName else str(len(list(FolderModel().childItems(
                        FolderModel().load(
                            docFolder,
                            level=None,
                            user=user,
                            force=True
                        ))
                    )) + 1)
                    if item:
                        item['name'] = name
                        item['folderId'] = docFolder['_id']
                        modelClass.updateItem(item)
                    else:
                        item = modelClass.createItem(
                            name=prefName if prefName else str(len(list(
                                FolderModel().childItems(
                                    FolderModel().load(
                                        docFolder,
                                        level=None,
                                        user=user,
                                        force=True
                                    )
                                )
                            )) + 1),
                            creator=user,
                            folder=docFolder,
                            reuseExisting=False
                        )

                        metadata['identifier'] = '{}/{}'.format(metadata['activityId'], str(item['_id']))

                        if editExisting:
                            insertHistoryData(None, '{}/{}'.format(metadata['activityId'], str(item['_id'])), modelType, baseVersion, historyFolder, historyReferenceFolder, user)

                    newModel = modelClass.setMetadata(
                        item,
                        {
                            **item.get('meta', {}),
                            **metadata
                        }
                    )

                update = {
                    'loadedFromSingleFile': True,
                    'lastUpdatedBy': user['_id']
                }
                if 'duplicateOf' in model['ref2Document']:
                    update['duplicateOf'] = ObjectId(model['ref2Document']['duplicateOf'])
                modelClass.update(
                    {'_id': newModel['_id']},
                    {'$set': update }
                )

                if modelType != 'protocol':
                    formatted = _fixUpFormat(formatLdObject(
                        newModel,
                        mesoPrefix=modelType,
                        user=user,
                        refreshCache=True
                    ))

                    createCache(newModel, formatted, modelType, user)

                model['_id'] = newModel['_id']

                if modelType == 'protocol':
                    protocolId = newModel['_id']
                    if editExisting and baseVersion and newModel.get('meta', {}).get('historyId', None):
                        historyFolder = FolderModel().load(newModel['meta']['historyId'], force=True)
                        historyReferenceFolder = FolderModel().load(historyFolder['meta']['referenceId'], force=True)

                        activityIdToHistoryObj = {}
                        # handle deleted activites
                        if 'activities' in removed:
                            removedActivities = list(ActivityModel().find({
                                'meta.protocolId': protocolId,
                                '_id': {
                                    '$in': [
                                        ObjectId(activityId) for activityId in removed['activities']
                                    ]
                                }
                            }))

                            for activity in removedActivities:
                                clearCache(activity, 'activity')
                                ActivityModel().remove(activity)

                                if 'identifier' in activity['meta']:
                                    activityId = str(activity['_id'])
                                    activityIdToHistoryObj[activityId] = insertHistoryData(activity, activity['meta']['identifier'], 'activity', baseVersion, historyFolder, historyReferenceFolder, user)

                        # handle deleted items
                        if 'items' in removed:
                            removedItems = list(ScreenModel().find({
                                'meta.protocolId': protocolId,
                                '_id': {
                                    '$in': [
                                        ObjectId(itemId) for itemId in removed['items']
                                    ]
                                }
                            }))

                            for item in removedItems:
                                clearCache(item, 'screen')
                                ScreenModel().remove(item)

                                if 'identifier' in item['meta']:

                                    activityId = str(item['meta']['activityId'])
                                    historyObj = activityIdToHistoryObj.get(activityId, None)

                                    if not historyObj:

                                        activity = FolderModel().load(activityId, force=True)
                                        historyObj = insertHistoryData(activity, activity['meta']['identifier'], 'activity', baseVersion, historyFolder, historyReferenceFolder, user)

                                        activityIdToHistoryObj[activityId] = historyObj

                                    item['meta'].update({
                                        'originalActivityId': item['meta']['activityId'],
                                        'activityId': historyObj['_id']
                                    })

                                    insertHistoryData(item, item['meta']['identifier'], 'screen', baseVersion, historyFolder, historyReferenceFolder, user)

                model['ref2Document']['_id'] = newModel['_id']

    return protocolId

def getUpdatedContent(updates, document):
    # document: previous version of protocol data
    # updates: contains only changes
    # retrieve: newDocument = document + updates

    document['contexts'] = updates['contexts']
    document['protocol']['data'] = updates['protocol']['data']

    removedItems = updates.get('removed', {}).get('items', [])
    removedActivities = updates.get('removed', {}).get('activities', [])

    activityUpdates = updates['protocol'].get('activities', {})
    activityID2Key = {
        str(activityUpdates[key]['data']['_id']): key for key in activityUpdates
    }

    activities = document['protocol']['activities']
    for key in list(dict.keys(activities)):
        activityId = str(activities[key]['data']['_id'])

        if activityId in removedActivities:
            activities.pop(key)

        if activityId in activityID2Key:
            activity = activities[activityID2Key[activityId]] = activities.pop(key)
            activityUpdate = activityUpdates[activityID2Key[activityId]]
            itemUpdates = activityUpdate.get('items', {})

            itemID2Key = {
                str(itemUpdates[key]['_id']): key for key in itemUpdates
            }
            items = activity.get('items', {})
            activity['data'] = activityUpdate['data']

            # handle updates on activity level
            for itemKey in list(dict.keys(items)):
                itemId = str(items[itemKey]['_id'])

                if itemId in removedItems:
                    items.pop(itemKey)
                if itemId in itemID2Key:
                    items[itemKey] = itemUpdates[itemID2Key[itemId]]
                    items[itemID2Key[itemId]] = items.pop(itemKey)
                    itemID2Key.pop(itemId)

            # handle newly inserted items
            for itemId in itemID2Key:
                itemKey = itemID2Key[itemId]

                items[itemKey] = itemUpdates[itemKey]

            activityID2Key.pop(activityId)

    # handle newly inserted activities
    for activityId in activityID2Key:
        key = activityID2Key[activityId]

        activities[key] = activityUpdates[key]

    return document

def cacheProtocolContent(protocol, document, user, editExisting=False):
    contentFolder = None
    if protocol.get('meta', {}).get('contentId', None):
        contentFolder = FolderModel().load(protocol['meta']['contentId'], force=True)
        contentFolder['name'] = 'content of ' + protocol['name']

        FolderModel().validate(contentFolder, allowRename=True)

    if not contentFolder:
        contentFolder = FolderModel().createFolder(
            name='content of ' + protocol['name'],
            parent=protocol,
            parentType='folder',
            public=False,
            creator=user,
            allowRename=True,
            reuseExisting=True
        )

        protocol['meta']['contentId'] = contentFolder['_id']
        FolderModel().setMetadata(protocol, protocol['meta'])

    contentFolder['lastUpdatedBy'] = user['_id']

    FolderModel().save(contentFolder)

    version = protocol.get('meta', {}).get('protocol', {}).get('schema:version', None)

    if version and len(version):
        version = version[0].get('@value', None)

        item = ItemModel().createItem(
            name='content of {} ({})'.format(protocol['name'], version),
            creator=user,
            folder=contentFolder
        )

        if editExisting and 'baseVersion' in document:
            latestItem = ItemModel().findOne({
                'folderId': contentFolder['_id'],
                'version': document['baseVersion']
            })

            latestDocument = json_util.loads(latestItem['content'])

            # item['updates'] = json_util.dumps(document)
            item['content'] = json_util.dumps(getUpdatedContent(document, latestDocument))
            item['baseVersion'] = document['baseVersion']
        else:
            item['content'] = json_util.dumps(document)
        item['version'] = version

        ItemModel().save(item)

def updateContributions(protocol, document, user):
    contributionFolder = None
    if protocol.get('meta', {}).get('contributionId', None):
        contributionFolder = FolderModel().load(protocol['meta']['contributionId'], force=True)
        contributionFolder['name'] = 'contribution of ' + protocol['name']

        FolderModel().validate(contributionFolder, allowRename=True)

    if not contributionFolder:
        contributionFolder = FolderModel().createFolder(
            name='contribution of ' + protocol['name'],
            parent=protocol,
            parentType='folder',
            public=False,
            creator=user,
            allowRename=True,
            reuseExisting=True
        )

        protocol['meta']['contributionId'] = contributionFolder['_id']
        FolderModel().setMetadata(protocol, protocol['meta'])

    ## log new contributions
    appletIdToAccountId = {}

    for activity in document['protocol']['activities'].values():
        if 'items' in activity:
            for item in activity['items'].values():
                if item.get('baseItemId', None) and item.get('baseAppletId', None):
                    ScreenModel().removeWithQuery({
                        'meta.baseItemId': ObjectId(item['baseItemId']),
                        'meta.baseAppletId': ObjectId(item['baseAppletId'])
                    })

                    contribution = ScreenModel().createItem(
                        name=f'contribution of {item.get("name", "")}',
                        creator=user,
                        folder=contributionFolder,
                        reuseExisting=False
                    )

                    baseItem = ScreenModel().load(item['baseItemId'], force=True)
                    contribution['meta'] = {
                        'baseItemId': baseItem['_id'],
                        'itemId': item['_id'],
                        'baseAppletId': ObjectId(item['baseAppletId']),
                        'created': baseItem['created']
                    }
                    appletId = str(contribution['meta']['baseAppletId'])

                    if appletId not in appletIdToAccountId:
                        applet = FolderModel().findOne({'_id': ObjectId(appletId)})
                        appletIdToAccountId[appletId] = applet['accountId']

                    contribution['meta']['baseAccountId'] = appletIdToAccountId[appletId]

                    if baseItem.get('cached', None):
                        cache = loadCache(baseItem['cached'])
                    else:
                        cache = _fixUpFormat(formatLdObject(
                            baseItem,
                            mesoPrefix='screen',
                            user=user
                        ))

                    contribution['content'] = json_util.dumps(cache)

                    ScreenModel().save(contribution)

    removedActivities = document.get('removed', {}).get('activities', [])
    removedItems = document.get('removed', {}).get('items', [])

    # remove contribution info for deleted activities
    for activityId in removedActivities:
        FolderModel().removeWithQuery({
            'meta.activityId': ObjectId(activityId),
            'parentId': contributionFolder['_id']
        })

    # remove contribution info for deleted items
    for itemId in removedItems:
        item = ScreenModel().findOne({
            'meta.itemId': ObjectId(itemId),
            'folderId': contributionFolder['_id']
        })
        if item:
            clearCache(item, 'screen')
            ScreenModel().remove(item)

def loadFromSingleFile(document, user, editExisting=False):
    if 'protocol' not in document or 'data' not in document['protocol']:
        raise ValidationException(
            'should contain protocol field in the json file.',
        )

    contexts = document.get('contexts', {})

    protocol = {
        'protocol': {},
        'activity': {},
        'screen': {}
    }

    expandedProtocol = expandObj(contexts, document['protocol']['data'])
    protocol['protocol'][expandedProtocol['@id']] = {
        'expanded': expandedProtocol,
        'ref2Document': document['protocol']['data']
    }

    protocolId = None
    for activity in document['protocol']['activities'].values():
        expandedActivity = expandObj(contexts, activity['data'])
        protocol['activity'][expandedActivity['@id']] = {
            'parentKey': 'protocol',
            'parentId': expandedProtocol['@id'],
            'expanded': expandedActivity,
            'ref2Document': activity['data']
        }

        if 'items' not in activity and not editExisting:
            raise ValidationException(
                'should contain at least one item in each activity.',
            )

        if 'items' in activity:
            for item in activity['items'].values():
                expandedItem = expandObj(contexts, item)
                protocol['screen']['{}.{}'.format(expandedActivity['@id'], expandedItem['@id'])] = {
                    'parentKey': 'activity',
                    'parentId': expandedActivity['@id'],
                    'expanded': expandedItem,
                    'ref2Document': item
                }

    protocolId = createProtocolFromExpandedDocument(protocol, user, editExisting, document.get('removed', {}), document.get('baseVersion', None))
    protocol = ProtocolModel().load(protocolId, force=True)

    cacheProtocolContent(protocol, document, user, editExisting)
    updateContributions(protocol, document, user)

    return formatLdObject(
        protocol,
        mesoPrefix='protocol',
        user=user,
        refreshCache=True
    )

def importAndCompareModelType(model, url, user, modelType, meta={}, existing=None):
    import threading
    from girderformindlogger.utility import firstLower

    if model is None:
        return(None, None)
    mt = model.get('@type', '')
    mt = mt[0] if isinstance(mt, list) else mt
    atType = mt.split('/')[-1].split(':')[-1]
    modelType = firstLower(atType) if len(atType) else modelType
    modelType = 'screen' if modelType.lower(
    )=='field' else 'protocol' if modelType.lower(
    )=='activityset' else modelType
    changedModel = (
        (atType != modelType and len(atType)) or (" " in modelType)
    )
    modelType = firstLower(atType) if changedModel else modelType
    modelType = 'screen' if modelType.lower(
    )=='field' else 'protocol' if modelType.lower(
    )=='activityset' else modelType
    modelClass = MODELS()[modelType]()
    prefName = modelClass.preferredName(model)
    cachedDocObj = {}
    model = expand(url)

    print("Loaded {}".format(": ".join([modelType, prefName])))
    docCollection=getModelCollection(modelType)
    if modelClass.name in ['folder', 'item']:
        docFolder = None

        if existing:
            if modelClass.name == 'folder':
                existing['name'] = prefName
                FolderModel().updateFolder(existing)
                docFolder = existing
            elif modelClass.name == 'item':
                docFolder = FolderModel().findOne({'_id': existing['folderId']})

                if docFolder:
                    docFolder['name'] = prefName
                    FolderModel().updateFolder(docFolder)

        if not docFolder:
            docFolder = FolderModel().createFolder(
                name=prefName,
                parent=docCollection,
                parentType='collection',
                public=True,
                creator=user,
                allowRename=True,
                reuseExisting=(modelClass.name=='item')
            )

        if modelClass.name=='folder':
            newModel = modelClass.setMetadata(
                docFolder,
                {
                    modelType: {
                        **model,
                        'schema:url': url,
                        'url': url
                    },
                    **meta
                }
            )
        elif modelClass.name=='item':
            item = None

            name = prefName if prefName else str(len(list(
                FolderModel().childItems(
                    FolderModel().load(
                        docFolder,
                        level=None,
                        user=user,
                        force=True
                    )
                )
            )) + 1)
            if existing:
                existing['name'] = name
                existing['folderId'] = docFolder['_id']
                modelClass.updateItem(existing)
                item = existing

            if not item:
                item = modelClass.createItem(
                    name=name,
                    creator=user,
                    folder=docFolder,
                    reuseExisting=False
                )

            newModel = modelClass.setMetadata(
                item,
                {
                    modelType: {
                        **model,
                        'schema:url': url,
                        'url': url
                    },
                    **meta
                }
            )

        modelClass.update(
            {'_id': newModel['_id']},
            {'$set': {
                'loadedFromSingleFile': False
            }}
        )
        newModel['loadedFromSingleFile'] = False

    formatted = _fixUpFormat(formatLdObject(
        newModel,
        mesoPrefix=modelType,
        user=user,
        refreshCache=True
    ))
    createCache(newModel, formatted, modelType, user)
    return(formatted, modelType)


def _createContextForStr(s):
    sp = s.split('/')
    k = '_'.join(
        sp[:-1] if '.' not in sp[-1] else sp
    ).replace('.','').replace(':','')
    return(
        (
            {k: '{}/'.format('/'.join(sp[:-1]))},
            "{}:{}".format(k, sp[-1])
        ) if '.' not in sp[-1] else (
            {k: s},
            k
        )
    )


def contextualize(ldObj):
    newObj = {}
    context = ldObj.get('@context', [])
    if isinstance(context, list):
        context.append(
            {
                "reprolib": REPROLIB_CANONICAL
            }
        )
    elif isinstance(context, dict):
        context["reprolib"] = REPROLIB_CANONICAL
    for k in ldObj.keys():
        if isinstance(ldObj[k], dict):
            context, newObj[k] = _deeperContextualize(
                ldObj[k],
                context
            )
        else:
            newObj[k] = ldObj[k]
    newObj['@context'] = reprolibCanonize(context)
    return(expand(newObj))


def _deeperContextualize(ldObj, context):
    newObj = {}
    for k in ldObj.keys():
        if isinstance(ldObj[k], dict) and '.' in k:
                (c, o) = _createContextForStr(k)
                newObj[o] = ldObj[k]
                if c not in context:
                    context.append(c)
        else:
            newObj[k] = reprolibPrefix(ldObj[k])
    return(context, newObj)


def childByParent(parent, applet, parentProfile=None):
    from girderformindlogger.models.profile import Profile

    parentProfile = Profile().getProfile(
        Profile().createProfile(
            applet['applet']['_id'].split('applet/')[-1],
            parent,
            "user"
        ).get('_id'),
        user=parent
    ) if parentProfile is None else parentProfile
    parentKnows = parentProfile.get('schema:knows', {})
    children = [Profile().displayProfileFields(
        Profile().load(
            p,
            force=True
        ),
        parent
    ) for p in list(
            set(parentKnows.get('rel:parentOf', {})).union(
                set(parentKnows.get('schema:children', {}))
            )
        )
    ]
    return([
        formatChildApplet(child, deepcopy(applet)) for child in children
    ])


def formatChildApplet(child, applet):
    applet['applet'] = _formatChildLabel(
        applet['applet'],
        child['displayName']
    )
    for act in applet['activities']:
        applet['activities'][act] = _formatChildLabel(
            applet['activities'][act],
            child['displayName']
        )
    return(_formatSubjectDocument(applet, child))


def _formatChildLabel(obj, label):
    for i, pl in enumerate(obj.get(
        "http://www.w3.org/2004/02/skos/core#prefLabel",
        []
    )):
        obj["http://www.w3.org/2004/02/skos/core#prefLabel"][i][
            "@value"
        ] = ": ".join([
            label,
            obj["http://www.w3.org/2004/02/skos/core#prefLabel"][
                i
            ].get("@value", "")
        ])
    return(obj)


def _formatSubjectDocument(obj, child):
    if isinstance(obj, str):
        return("?subjectId=".join([obj, str(child['_id'])]) if (
            obj.startswith("reprolib:") and not (
                obj.startswith("reprolib:terms") or obj.split(":")[-1].isupper()
            )
        ) else obj)
    elif isinstance(obj, list):
        return([_formatSubjectDocument(i, child) for i in obj])
    elif isinstance(obj, dict):
        n = {}
        for k in obj.keys():
            nk = "?subjectId=".join([k, str(child['_id'])]) if (
                k.startswith("reprolib:") and not (
                    k.startswith("reprolib:terms") or k.split(":")[-1].isupper()
                )
            ) else k
            n[nk] = (
                "?subjectId=".join([
                    obj[k],
                    str(child['_id'])
                ]) if isinstance(obj[k], str) else [
                    "?subjectId=".join([
                        o,
                        str(child['_id'])
                    ]) for o in obj[k]
                ] if isinstance(obj[k], list) else obj[k]
            ) if k in [
                "@index",
                "order"
            ] else {
                "?subjectId=".join([
                    adnK,
                    str(child['_id'])
                ]): ": ".join([
                    child['displayName'],
                    obj[k][adnK]
                ]) for adnK in obj[k]
            } if k=="activity_display_name" else {
                "?subjectId=".join([
                    vizK,
                    str(child['_id'])
                ]): obj[k][vizK] for vizK in obj[k]
            } if k=="visibility" else obj[
                k
            ] if k=="@type" else _formatSubjectDocument(obj[k], child)
        return(n)
    else:
        return(obj)


def inferRelationships(person):
    from girderformindlogger.models.invitation import Invitation
    from girderformindlogger.models.profile import Profile

    if "schema:knows" not in person:
        return(person)
    start = deepcopy(person)
    for rel in list(person['schema:knows'].keys()):
        if rel in DEFINED_RELATIONS.keys():
            if "owl:equivalentProperty" in DEFINED_RELATIONS[rel]:
                for ep in DEFINED_RELATIONS[rel]["owl:equivalentProperty"]:
                    if ep not in person['schema:knows']:
                        person['schema:knows'][ep] = []
                    for related in person['schema:knows'][rel]:
                        if related not in person['schema:knows'][ep]:
                            person['schema:knows'][ep].append(related)
    if any([
        bool(
            rp not in start.get('schema:knows', {}).get(rel, [])
        ) for rp in person['schema:knows'][rel] for rel in list(
            person['schema:knows'].keys()
        )
    ]):
        newPerson = Profile().load(person['_id'], force=True)
        if 'schema:knows' in newPerson:
            newPerson['schema:knows'].update(person['schema:knows'])
        else:
            newPerson['schema:knows'] = person['schema:knows']
        Profile().save(
            newPerson,
            validate=False
        ) if 'userId' in newPerson else Invitation().save(
            newPerson,
            validate=False
        )
    return(person)


def oidIffHex(s):
    """
    Function to return a list of the passed string and its ObjectId if the
    passed string is a valid hexidecimal string, or a list of just the passed
    string otherwise.

    :param s: string to check and potentially convert
    :type s: str
    :returns: list of strings, 1≤len≤2
    """
    from bson.objectid import ObjectId
    from bson.errors import InvalidId

    try:
        ObjectId(s)
        return([ObjectId(s), s])
    except InvalidId:
        return([s])


def reprolibPrefix(s):
    """
    Function to check if a string is a reprolib URL, and, if so, compact it to
    the prefix "reprolib:"

    :type s: str
    :returns: str
    """
    if isinstance(s, str):
        for prefix in REPROLIB_PREFIXES:
            if s.startswith(prefix) and s!=prefix:
                return(s.replace(prefix, 'reprolib:'))
    elif isinstance(s, dict):
        for k in s.keys():
            s[k] = reprolibPrefix(
                s[k]
            ) if k not in KEYS_TO_DEREFERENCE else dereference(s[k])
    elif isinstance(s, list):
        s = [reprolibPrefix(li) for li in s]
    return(s)


def schemaPrefix(s):
    """
    Function to toggle between "schema:" and "http://schema.org/" prefixes.

    :type s: str
    :returns: str
    """
    a = "schema:"
    b = "http://schema.org/"
    if isinstance(s, str):
        if s.startswith(b):
            return(s.replace(b, a))
    return(s)


def reprolibCanonize(s):
    """
    Function to check if a string is a prfixed reprolib URL, and, if so,
    expand it to the current canonical prefix

    :type s: str
    :returns: str
    """
    if isinstance(s, str):
        s = reprolibPrefix(s).replace('reprolib:', REPROLIB_CANONICAL)
        return(s)
        ##
        ##Temporary disabled
        ##
        #if checkURL(s):
        #    return(s)
        #else:
        #    return(None)
    elif isinstance(s, list):
        return([reprolibCanonize(ls) for ls in s])
    elif isinstance(s, dict):
        return({
            reprolibCanonize(
                k
            ) if reprolibCanonize(
                k
            ) is not None else k: reprolibCanonize(v) for k, v in s.items()
        })
    return(s)


def delanguageTag(obj):
    """
    Function to take a language-tagged list of dicts and return an untagged
    string.

    :param obj: list of language-tagged dict
    :type obj: list
    :returns: string
    """
    if not isinstance(obj, list):
        return(obj)

    data = (obj if len(obj) else [{}])[-1]
    return data['@value'] if data.get('@value', '') else data.get('@id', '')

def expandOneLevel(obj):
    if obj is None:
        # We only want to catch `None`s here, not other falsy objects
        return(obj)
    try:
        if isinstance(obj, str):
            data = loadJSON(obj)
            if isinstance(data, dict):
                data = loadJSON(reprolibCanonize(obj)) if len(data.keys()) == 0 else data

                if '@context' in data:
                    if isinstance(data['@context'], list):
                        data['@context'][0] = 'https://raw.githubusercontent.com/jj105/reproschema-context/master/context.json'
                    if isinstance(data['@context'], str):
                        data['@context'] = 'https://raw.githubusercontent.com/jj105/reproschema-context/master/context.json'

                newObj = jsonld.expand(data)
            else:
                print("Invalid Url: ", obj)
                return (obj)
        else:
            newObj = jsonld.expand(obj)
    except jsonld.JsonLdError as e: # 👮 Catch illegal JSON-LD
        if e.cause.type == "jsonld.ContextUrlError":
            invalidContext = e.cause.details.get("url")
            print("Invalid context: {}".format(invalidContext))
            if isinstance(obj, str):
                obj = loadJSON(obj)
            if not isinstance(obj, dict):
                obj = {"@context": []}
            if invalidContext in obj.get("@context", []):
                obj["@context"] = obj["@context"].remove(invalidContext)
                obj["@context"].append(reprolibCanonize(invalidContext))
                if obj["@context"] is None:
                    obj["@context"] = []
            else:
                if isinstance(obj, dict):
                    for k in obj.keys():
                        if invalidContext in obj[k].get("@context", []):
                            obj[k]["@context"] = obj[k]["@context"].remove(
                                invalidContext
                            )
                            if obj[k]["@context"] is None:
                                obj[k]["@context"] = []
                            obj[k]["@context"].append(reprolibCanonize(
                                invalidContext
                            ))
            return(expandOneLevel(obj))
        return(obj)
    newObj = newObj[0] if (
        isinstance(newObj, list) and len(newObj)==1
    ) else newObj
    if isinstance(
        newObj,
        dict
    ):
        if not isinstance(obj, dict) or '@context' in obj:
            obj={}
        for k, v in deepcopy(newObj).items():
            if not bool(v):
                newObj.pop(k)
            else:
                prefix_key = reprolibPrefix(k)
                if prefix_key != k:
                    newObj.pop(k)
                newObj[prefix_key] = reprolibPrefix(
                    v
                ) if prefix_key not in KEYS_TO_DEREFERENCE else dereference(v)
        for k in KEYS_TO_DELANGUAGETAG:
            if k in newObj.keys(
            ) and isinstance(newObj[k], list):
                newObj[k] = delanguageTag(newObj[k])
        newObj.update({
            k: reprolibPrefix(obj.get(k)) for k in obj.keys() if (
                bool(obj.get(k)) and k not in keyExpansion(
                    list(newObj.keys())
                )
            )
        })
        newObj.update({
            k: dereference(newObj[k]) for k in newObj.keys(
            ) if k in KEYS_TO_DEREFERENCE
        })
    return(newObj)


def dereference(prefixed):
    """
    Function to dereference values in given JSON.

    :param prefixed: JSON to dereference
    :type prefixed: dict, list, str, or None
    :returns: dereferenced same-type
    """
    if isinstance(prefixed, str):
        d = reprolibCanonize(prefixed)
        return(d if d is not None else prefixed)
    elif isinstance(prefixed, dict):
        return({
            k: dereference(v) for k, v in prefixed.items()
        })
    elif isinstance(prefixed, list):
        return([dereference(li) for li in prefixed])
    else: # bool, int, float, None
        return(prefixed)


def expand(obj, keepUndefined=False):
    """
    Function to take an unexpanded JSON-LD Object and return it expandeds.

    :param obj: unexpanded JSON-LD Object
    :type obj: dict
    :param keepUndefined: keep undefined-in-context terms?
    :param keepUndefined: bool
    :returns: list, expanded JSON-LD Array or Object
    """
    if obj is None:
        return(obj)

    newObj = expandOneLevel(obj)

    if isinstance(newObj, dict):
        for k in KEYS_TO_EXPAND:
            if k in newObj.keys():
                if isinstance(newObj.get(k), list):
                    v = [
                        expand(lv.get('@id')) for lv in newObj.get(k)
                    ]
                    v = v if v!=[None] else None
                else:
                    v = expand(newObj[k])
                if bool(v):
                    newObj[k] = delanguageTag(
                        v
                    ) if k in KEYS_TO_DELANGUAGETAG else dereference(
                        v
                    ) if k in KEYS_TO_DEREFERENCE else reprolibPrefix(v)
        if k in KEYS_TO_DELANGUAGETAG:
            if k in newObj:
                newObj[k] = reprolibCanonize(
                    delanguageTag(newObj[k])
                )
        if k in KEYS_TO_DEREFERENCE:
            if k in newObj:
                newObj[k] = dereference(newObj[k])
        return(_fixUpFormat(newObj) if bool(newObj) else None)
    else:
        expanded = [expand(n, keepUndefined) for n in newObj]
        return(_fixUpFormat(expanded) if bool(expanded) else None)


def fileObjectToStr(obj):
    """
    Function to load a linked file in a JSON-LD object and return a string.

    :param obj: Object
    :type obj: dict
    :returns: String from loaded file
    """
    import requests
    from requests.exceptions import ConnectionError, MissingSchema
    try:
        r = requests.get(obj.get('@id'))
    except (AttributeError, ConnectionError, MissingSchema):
        r = obj.get("@id") if isinstance(obj, dict) else ""
        raise ResourcePathNotFound("Could not load {}".format(r))
    return(r.text)


def checkURL(s):
    """
    Function to check if a URL is dereferenceable

    :param s: URL
    :type s: string
    :returns: bool
    """
    import requests
    try:
        if (requests.get(s).status_code==404):
            return(False)
        else:
            return(True)
    except:
        return(False)


def compactKeys(obj):
    context = obj.get('@context', [])
    if not isinstance(context, list):
        context = [context]
    newObj = {}
    for k in list(obj.keys()):
        if "." in k:
            c, nk = _createContext(k)
            if c not in context:
                context.append(c)
            nc, newObj[nk] = _deepCompactKeys(obj[k])
            for c in nc:
                if c not in context:
                    context.append(c)
        else:
            nc, newObj[k] = _deepCompactKeys(obj[k])
            for c in nc:
                if c not in context:
                    context.append(c)
    newObj['@context'] = context
    return(newObj)


def _deepCompactKeys(obj):
    context = []
    if not isinstance(obj, dict):
        return(context, obj)
    newObj = {}
    for k in list(obj.keys()):
        if "." in k:
            c, nk = _createContext(k)
            if c not in context:
                context.append(c)
            nc, newObj[nk] = _deepCompactKeys(obj[k])
            for c in nc:
                if c not in context:
                    context.append(c)
        else:
            nc, newObj[k] = _deepCompactKeys(obj[k])
            for c in nc:
                if c not in context:
                    context.append(c)
    return(context, newObj)


def _createContext(key):
    s = key.split('/')
    k = s[-1]
    key = '{}/'.format('/'.join(s[0:-1]))
    return({key.split('://')[-1].replace('.', '_dot_'): key}, k)


def createCache(obj, formatted, modelType, user = None):
    obj = MODELS()[modelType]().load(obj['_id'], force=True)
    if modelType in NONES:
        print("No modelType!")
        print(obj)
    if formatted is None:
        print("formatting failed!")
        print(obj)

    if obj.get('cached'):
        cache_id = obj['cached']
        CacheModel().updateCache(cache_id, MODELS()[modelType]().name, obj['_id'], modelType, formatted)
    else:
        obj['updated'] = datetime.utcnow()

        if modelType == 'applet':
            formatted['updated'] = obj['updated']

        saved = CacheModel().insertCache(MODELS()[modelType]().name, obj['_id'], modelType, formatted)
        obj['cached'] = saved['_id']

        MODELS()[modelType]().update({
            '_id': ObjectId(obj['_id'])
        }, {
            '$set': {
                'cached': obj['cached'],
                'updated': obj['updated']
            }
        }, False)

    return obj

def clearCache(obj, modelType):
    if modelType in NONES:
        print("No modelType!")
        print(obj)

    obj = MODELS()[modelType]().load(obj['_id'], force=True)

    if obj.get('cached'):
        cache_id = obj['cached']
        obj['cached'] = None
        MODELS()[modelType]().update({'_id': ObjectId(obj['_id'])}, {'$set': {'cached': None}}, False)
        CacheModel().removeWithQuery({'_id': ObjectId(cache_id)})
    return obj

def loadCache(id):
    cache = CacheModel().getCacheData(id)
    return cache

def _fixUpFormat(obj):
    if isinstance(obj, dict):
        newObj = {}
        for k in obj.keys():
            rk = reprolibPrefix(k)
            if k in KEYS_TO_DELANGUAGETAG:
                newObj[rk] = reprolibCanonize(
                    delanguageTag(obj[k])
                )
            elif k in KEYS_TO_DEREFERENCE:
                newObj[rk] = dereference(obj[k])
            elif isinstance(obj[k], list):
                newObj[rk] = [_fixUpFormat(li) for li in obj[k]]
            elif isinstance(obj[k], dict):
                newObj[rk] = _fixUpFormat(obj[k])
            else: # bool, int, float
                newObj[rk] = obj[k]
            if isinstance(obj[k], str) and k not in KEYS_TO_DEREFERENCE:
                c = reprolibPrefix(obj[k])
                newObj[rk] = c if c is not None else obj[k]
            s2k = schemaPrefix(rk)
            if s2k!=rk:
                newObj[s2k] = newObj.pop(rk)
        if "@context" in newObj:
            newObj["@context"] = reprolibCanonize(newObj["@context"])
        for k in ["schema:url", "http://schema.org/url"]:
            if k in newObj and newObj[k] is not None:
                newObj["url"] = newObj["schema:url"] = newObj[k]
        return(newObj)
    elif isinstance(obj, str):
        return(reprolibPrefix(obj))
    else:
        return(obj)

def fixUpOrderList(obj, modelType, dictionary):
    updated = False
    if "reprolib:terms/order" in obj['meta'].get(modelType, {}):
        order = obj['meta'][modelType]["reprolib:terms/order"][0]["@list"]
        for child in order:
            uri = child.get("@id", None)

            if modelType != 'screen':
                key = '{}/{}'.format(obj['_id'], uri.split("/")[-1])
                if key in dictionary:
                    child["@id"] = dictionary[key]
                    updated = True

    return updated

def formatLdObject(
    obj,
    mesoPrefix='folder',
    user=None,
    keepUndefined=False,
    dropErrors=False,
    refreshCache=False,
    responseDates=False
):
    """
    Function to take a compacted JSON-LD Object within a Girder for Mindlogger
    database and return an exapanded JSON-LD Object including an _id.

    :param obj: Compacted JSON-LD Object
    :type obj: dict or list
    :param mesoPrefix: Girder for Mindlogger entity type, defaults to 'folder'
                       if not provided
    :type mesoPrefix: str
    :param user: User making the call
    :type user: User
    :param keepUndefined: Keep undefined properties
    :type keepUndefined: bool
    :param dropErrors: Return `None` instead of raising an error for illegal
        JSON-LD definitions.
    :type dropErrors: bool
    :param refreshCache: Refresh from Dereferencing URLs?
    :type refreshCache: bool
    :param responseDates: Include list of ISO date strings of responses
    :type responseDates: bool
    :returns: Expanded JSON-LD Object (dict or list)
    """
    from girderformindlogger.models import pluralize

    refreshCache = False if refreshCache is None else refreshCache

    try:
        if obj is None:
            return(None)
        if isinstance(obj, dict):
            oc = obj.get("cached")
            if all([
                not refreshCache,
                oc is not None
            ]):
                return(loadCache(oc))
            if 'meta' not in obj.keys():
                return(_fixUpFormat(obj))
        mesoPrefix = camelCase(mesoPrefix)
        if type(obj)==list:
            return(_fixUpFormat([
                formatLdObject(
                    o,
                    mesoPrefix,
                    refreshCache=refreshCache,
                    user=user
                ) for o in obj if o is not None
            ]))
        if not type(obj)==dict and not dropErrors:
            raise TypeError("JSON-LD must be an Object or Array.")
        newObj = obj.get('meta', obj)
        newObj = newObj.get(mesoPrefix, newObj)

        if not obj.get('loadedFromSingleFile', False):
            newObj = expand(newObj, keepUndefined=keepUndefined)

        if type(newObj)==list and len(newObj)==1:
            try:
                newObj = newObj[0]
            except:
                raise ValidationException(str(newObj))
        if type(newObj)!=dict:
            newObj = {}
        objID = str(obj.get('_id', 'undefined'))
        if objID=='undefined':
            raise ResourcePathNotFound('unable to load object')
        newObj['_id'] = "/".join([snake_case(mesoPrefix), objID])
        if mesoPrefix=='applet':
            protocolUrl = obj.get('meta', {}).get('protocol', obj).get(
                'http://schema.org/url',
                obj.get('meta', {}).get('protocol', obj).get('url')
            )

            # get protocol data from id
            protocol = None
            protocolId = obj.get('meta', {}).get('protocol', {}).get('_id' ,'').split('/')[-1]
            if protocolId:
                cache = ProtocolModel().getCache(protocolId)
                if cache:
                    protocol = loadCache(cache)

            if protocolUrl is not None and not protocol:
                # get protocol from url
                protocol = ProtocolModel().load(ObjectId(protocolId), user, force=True)

                if 'appletId' not in protocol.get('meta', {}):
                    protocol['meta']['appletId'] = 'None'
                    ProtocolModel().setMetadata(protocol, protocol['meta'])

                protocol = ProtocolModel().getFromUrl(
                            protocolUrl,
                            'protocol',
                            user,
                            thread=False,
                            refreshCache=refreshCache,
                            meta={
                                'appletId': protocol['meta']['appletId']
                            }
                        )[0]

            # format protocol data
            protocol = formatLdObject(
                protocol,
                'protocol',
                user,
                refreshCache=refreshCache
            )

            applet = {}
            applet['activities'] = protocol.pop('activities', {})
            applet['items'] = protocol.pop('items', {})
            applet['protocol'] = {
                key: protocol.get(
                    'protocol',
                    protocol.get(
                        'activitySet',
                        {}
                    )
                ).pop(
                    key
                ) for key in [
                    '@type',
                    '_id',
                    'http://schema.org/url',
                    'schema:url',
                    'url'
                ] if key in list(protocol.get('protocol', {}).keys())
            }
            applet['applet'] = {
                **protocol.pop('protocol', {}),
                **obj.get('meta', {}).get(mesoPrefix, {}),
                'encryption': obj.get('meta', {}).get('encryption', {}),
                'retentionSettings': obj.get('meta', {}).get('retentionSettings', {}),
                '_id': "/".join([snake_case(mesoPrefix), objID]),
                'url': "#".join([
                    obj.get('meta', {}).get('protocol', {}).get("url", "")
                ])
            }

            if len(obj.get('meta', {}).get('applet', {}).get('displayName', '')):
                inserted = False

                candidates = ['prefLabel', 'altLabel']
                for candidate in candidates:
                    for key in applet['applet']:
                        if not inserted and str(key).endswith(candidate) and len(applet['applet'][key]) and len(applet['applet'][key][0].get('@value', '')):
                            applet['applet'][key][0]['@value'] = obj['meta']['applet']['displayName']

                            inserted = True

            createCache(obj, applet, 'applet', user)
            if responseDates:
                try:
                    applet["applet"]["responseDates"] = responseDateList(
                        obj.get('_id'),
                        user.get('_id'),
                        user
                    )
                except:
                    applet["applet"]["responseDates"] = []
            return(applet)
        elif mesoPrefix=='protocol':
            protocol = {
                'protocol': newObj,
                'activities': {},
                "items": {}
            }

            if obj.get('loadedFromSingleFile', False):
                activities = list(ActivityModel().find({'meta.protocolId': obj['_id']}))
                items = list(ScreenModel().find({'meta.protocolId': obj['_id']}))

                itemIDMapping = {}
                activityIDMapping = {}

                for item in items:
                    formatted = formatLdObject(item, 'screen', user)
                    key = '{}/{}'.format(str(item['meta']['activityId']), str(item['_id']))

                    itemIDMapping['{}/{}'.format(str(item['meta']['activityId']), formatted['@id'])] = key
                    if item.get('duplicateOf', None):
                        itemIDMapping['{}/{}'.format(str(item['meta']['activityId']), str(item['duplicateOf']))] = key

                    protocol['items'][key] = formatted

                for activity in activities:
                    if refreshCache and fixUpOrderList(activity, 'activity', itemIDMapping):
                        ActivityModel().setMetadata(activity, activity['meta'])

                        formatted = formatLdObject(activity, 'activity', user, refreshCache=True)
                        createCache(activity, formatted, 'activity', user)
                    else:
                        formatted = formatLdObject(activity, 'activity', user)

                    protocol['activities'][str(activity['_id'])] = formatted

                    activityIDMapping['{}/{}'.format(str(obj['_id']), formatted['@id'])] = str(activity['_id'])

                if refreshCache:
                    if fixUpOrderList(obj, 'protocol', activityIDMapping):
                        ProtocolModel().setMetadata(obj, obj['meta'])
            else:
                try:
                    protocol = componentImport(
                        newObj,
                        deepcopy(protocol),
                        user,
                        refreshCache=refreshCache,
                        meta={'protocolId': ObjectId(obj['_id'])}
                    )
                except:
                    print("636")
                    protocol = componentImport(
                        newObj,
                        deepcopy(protocol),
                        user,
                        refreshCache=True
                    )

                newActivities = protocol.get('activities', {}).keys()
                newItems = protocol.get('items', {}).keys()

                while(any([len(newActivities), len(newItems)])):
                    activitiesNow = set(
                        protocol.get('activities', {}).keys()
                    )
                    itemsNow = set(protocol.get('items', {}).keys())
                    for activityURL in newActivities:
                        activity = protocol['activities'][activityURL]
                        activity = activity.get(
                            'meta',
                            {}
                        ).get('activity', activity)
                        try:
                            protocol = componentImport(
                                deepcopy(activity),
                                deepcopy(protocol),
                                user,
                                refreshCache=refreshCache,
                                meta={'protocolId': ObjectId(obj['_id']), 'activityId': ObjectId(protocol['activities'][activityURL]['_id'].split('/')[-1])}
                            )
                        except:
                            print("670")
                            protocol = componentImport(
                                deepcopy(activity),
                                deepcopy(protocol),
                                user,
                                refreshCache=True,
                                meta={'protocolId': obj['_id'], 'activityId': activity['_id']}
                            )
                    for itemURL in newItems:
                        activity = protocol['items'][itemURL]
                        activity = activity.get(
                            'meta',
                            {}
                        ).get('screen', activity)
                        try:
                            protocol = componentImport(
                                deepcopy(activity),
                                deepcopy(protocol),
                                user,
                                refreshCache=refreshCache
                            )
                        except:
                            print("691")
                            protocol = componentImport(
                                deepcopy(activity),
                                deepcopy(protocol),
                                user,
                                refreshCache=True
                            )
                    newActivities = list(
                        set(
                            protocol.get('activities', {}).keys()
                        ) - activitiesNow
                    )
                    newItems = list(
                        set(
                            protocol.get('items', {}).keys()
                        ) - itemsNow
                    )

            formatted = _fixUpFormat(protocol)

            createCache(obj, formatted, 'protocol')
            return formatted
        else:
            return(_fixUpFormat(newObj))
    except:
        if refreshCache==False:
            return(_fixUpFormat(formatLdObject(
                obj,
                mesoPrefix,
                user,
                keepUndefined,
                dropErrors,
                refreshCache=True,
                responseDates=responseDates
            )))
        import sys, traceback
        print(sys.exc_info())
        print(traceback.print_tb(sys.exc_info()[2]))


def componentImport(
    obj,
    protocol,
    user=None,
    refreshCache=False,
    modelType=['activity', 'item'],
    meta={}
):
    """
    :param modelType: model or models to search
    :type modelType: str or iterable
    :returns: protocol (updated)
    """
    import itertools
    from girderformindlogger.models import pluralize, smartImport
    from girderformindlogger.utility import firstLower

    updatedProtocol = deepcopy(protocol)
    obj2 = {k: v for k, v in expand(deepcopy(obj)).items() if v is not None}
    try:
        for order in obj2.get(
            "reprolib:terms/order",
            {}
        ):
            for activity in order.get("@list", []):
                IRI = activity.get(
                    'url',
                    activity.get('@id')
                )
                if reprolibPrefix(IRI) not in list(
                    itertools.chain.from_iterable([
                        protocol.get(mt, {}).keys() for mt in [
                            "activities",
                            "items"
                        ]
                    ])
                ):
                    result =         \
                        smartImport(
                            IRI,
                            user=user,
                            refreshCache=refreshCache,
                            meta=meta
                        ) if (IRI is not None and not IRI.startswith(
                            "Document not found"
                        )) else (None, None, None)
                    activityComponent, activityContent, canonicalIRI = result

                    activity["url"] = activity["schema:url"] = canonicalIRI if(
                        canonicalIRI is not None
                    ) else IRI
                    activityComponent = pluralize(firstLower(
                        activityContent.get(
                            '@type',
                            ['']
                        )[0].split('/')[-1].split(':')[-1]
                    )) if (activityComponent is None and isinstance(
                        activityContent,
                        dict
                    )) else activityComponent
                    if activityComponent is not None:
                        activityComponents = (
                            pluralize(
                                activityComponent
                            ) if activityComponent != 'screen' else 'items'
                        )
                        updatedProtocol[activityComponents][
                            canonicalIRI
                        ] = deepcopy(formatLdObject(
                            activityContent,
                            activityComponent,
                            user,
                            refreshCache=refreshCache
                        ))
        return(updatedProtocol.get(
            'meta',
            updatedProtocol
        ).get(modelType if isinstance(
            modelType,
            str
        ) else modelType[0], updatedProtocol))
    except:
        import sys, traceback
        print("error!")
        print(sys.exc_info())
        print(traceback.print_tb(sys.exc_info()[2]))


def getByLanguage(object, tag=None):
    """
    Function to get a value or IRI by a language tag following
    https://tools.ietf.org/html/bcp47.

    :param object: The JSON-LD Object to language-parse
    :type object: dict or list
    :param tag: The language tag to use.
    :type tag: str
    :returns: str, either a literal or an IRI.
    """
    if not tag:
        from girderformindlogger.api.v1.context import Context
        tag = FolderModel().findOne({
            'name': 'JSON-LD',
            'parentCollection': 'collection',
            'parentId': CollectionModel().findOne({
                'name': 'Context'
            }).get('_id')
        })
        tag = tag.get('meta', {}).get('@context', {}).get(
            '@language'
        ) if tag else None
    if isinstance(tag, str):
        tags = getMoreGeneric(tag)
        tags = tags + ["@{}".format(t) for t in tags]
        tags.sort(key=len, reverse=True)
        if isinstance(object, dict):
            return(
                getFromLongestMatchingKey(object, tags, caseInsensitive=True)
            )
        if isinstance(object, list):
            return([getFromLongestMatchingValue(
                objectList=object,
                listOfValues=tags,
                keyToMatch='@language',
                caseInsensitive=True
            )])
    if isinstance(object, str):
        return(object)


def getFromLongestMatchingKey(object, listOfKeys, caseInsensitive=True):
    """
    Function to take an object and a list of keys and return the value of the
    longest matching key or None if no key matches.

    :param object: The object with the keys.
    :type object: dict
    :param listOfKeys: A list of keys to try to match
    :type listOfKeys: list of string keys
    :param caseInsensitive: Case insensitive key matching?
    :type caseInsensitive: boolean
    :returns: value of longest matching key in object
    """
    listOfKeys = listOfKeys.copy()
    if caseInsensitive:
        object = {k.lower():v for k,v in object.items()}
        listOfKeys = [k.lower() for k in listOfKeys]
    key = max(
        [str(k) for k in listOfKeys],
        key=len
    ) if len(listOfKeys) else None
    if key and key in listOfKeys:
        listOfKeys.remove(key)
    return(
        object.get(
            key,
            getFromLongestMatchingKey(object, listOfKeys)
        ) if key else None
    )


def getFromLongestMatchingValue(
    objectList,
    listOfValues,
    keyToMatch,
    caseInsensitive=True
):
    """
    Function to take a list of objects, a list of values and a key to match and
    return the object with the longest matching value for that key or None if
    no value matches for that that key.

    :param objectList: The list of objects.
    :type objectList: list of dicts
    :param listOfValues: A list of values to try to match
    :type listOfValues: list of string values
    :param keyToMatch: key in which to match the value
    :type keyToMatch: str
    :param caseInsensitive: Case insensitive value matching?
    :type caseInsensitive: boolean
    :returns: dict with longest matching value for specified key in object
    """
    objectList = objectList.copy()
    if caseInsensitive:
        listOfValues = [k.lower() for k in listOfValues]
    value = max(
        [str(k) for k in listOfValues],
        key=len
    ) if len(listOfValues) else None
    if value and value in listOfValues:
        listOfValues.remove(value)
    for object in sorted(
        objectList,
        key=lambda i: len(i.get(keyToMatch, "")),
        reverse=True
    ):
        if (
            object.get(keyToMatch, '').lower(
            ) if caseInsensitive else object.get(keyToMatch, '')
        )==value:
            return(object)
    if len(listOfValues)>=1:
        return(getFromLongestMatchingValue(
            objectList,
            listOfValues,
            keyToMatch,
            caseInsensitive
        ))
    for object in sorted(
        objectList,
        key=lambda i: len(i.get(keyToMatch, "")),
        reverse=False
    ):
        generic = object.get(keyToMatch, '').lower(
        ) if caseInsensitive else object.get(keyToMatch, '')
        generic = generic.split('-')[0] if '-' in generic else generic
        if generic==value:
            return(object)
    return({})


def getMoreGeneric(langTag):
    """
    Function to return a list of decreasingly specific language tags, given a
    language tag.

    :param langTag: a language tag following https://tools.ietf.org/html/bcp47
    :type langTag: str
    :returns: list
    """
    langTags = [langTag]
    while '-' in langTag:
        langTag = langTag[::-1].split('-', 1)[1][::-1]
        langTags.append(langTag)
    return(langTags)


def keyExpansion(keys):
    return(list(set([
        k.split(delimiter)[-1] for k in keys for delimiter in [
            ':',
            '/'
        ] if delimiter in k
    ] + [
        k for k in keys if (':' not in k and '/' not in k)
    ])))


def camelCase(snake_case):
    """
    Function to convert a snake_case_string to a camelCaseString

    :param snake_case: snake_case_string
    :type snake_case: str
    :returns: camelCaseString
    """
    words = snake_case.split('_')
    return('{}{}'.format(
        words[0],
        ''.join([
            word.title() for word in words[1:]
        ])
    ))

def snake_case(camelCase):
    """
    Function to convert a camelCaseString to a snake_case_string

    :param camelCase: camelCaseString
    :type camelCase: str
    :returns: snake_case_string
    """
    import re
    first_cap_re = re.compile('(.)([A-Z][a-z]+)')
    all_cap_re = re.compile('([a-z0-9])([A-Z])')
    return(
        all_cap_re.sub(
            r'\1_\2',
            first_cap_re.sub(
                r'\1_\2',
                camelCase
            )
        ).lower()
    )

def xsdNow():
    """
    Function to return an XSD formatted datetime string for the current
    datetime.now()
    """
    return(datetime.now(datetime.utcnow().astimezone().tzinfo).isoformat())
