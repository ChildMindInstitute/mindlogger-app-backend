# -*- coding: utf-8 -*-
import copy
import datetime
import json
import os
import six

from bson.objectid import ObjectId
from .model_base import AccessControlledModel
from girderformindlogger import events
from girderformindlogger.constants import AccessType
from girderformindlogger.exceptions import ValidationException, GirderException
from girderformindlogger.utility.model_importer import ModelImporter
from girderformindlogger.utility.progress import noProgress, \
    setResponseTimeLimit


class Invitation(AccessControlledModel):
    """
    Invitations store customizable information specific to both users and
    applets. These data can be sensitive and are access controlled.
    """

    def initialize(self):
        self.name = 'invitation'
        self.ensureIndices(('appletId', ([('appletId', 1)], {})))

        self.exposeFields(level=AccessType.READ, fields=(
            '_id', 'created', 'updated', 'meta', 'appletId',
            'parentCollection', 'creatorId', 'baseParentType', 'baseParentId'
        ))


    def load(self, id, level=AccessType.ADMIN, user=None, objectId=True,
             force=False, fields=None, exc=False):
        """
        We override load in order to ensure the folder has certain fields
        within it, and if not, we add them lazily at read time.

        :param id: The id of the resource.
        :type id: string or ObjectId
        :param user: The user to check access against.
        :type user: dict or None
        :param level: The required access type for the object.
        :type level: AccessType
        :param force: If you explicitly want to circumvent access
                      checking on this resource, set this to True.
        :type force: bool
        """
        # Ensure we include extra fields to do the migration below
        extraFields = {
            'appletId',
            'userId'
        }
        loadFields = self._supplementFields(fields, extraFields)

        doc = super(Invitation, self).load(
            id=id, level=level, user=user, objectId=objectId, force=force,
            fields=loadFields, exc=exc
        )

        if doc is not None:

            self._removeSupplementalFields(doc, fields)

        return doc


    def remove(self, invitation, progress=None, **kwargs):
        """
        Delete an invitation.

        :param invitation: The invitation document to delete.
        :type invitation: dict
        """
        # Delete this invitation
        AccessControlledModel.remove(
            self,
            invitation,
            progress=progress,
            **kwargs
        )
        if progress:
            progress.update(increment=1, message='Deleted invitation')

    def createInvitation(
        self,
        applet,
        coordinator,
        role="user",
        profile=None,
        idCode=None
    ):
        """
        Create a new invitation to store information specific to a given (applet
            ∩ (ID code ∪ profile))

        :param applet: The applet for which this invitation exists
        :type parent: dict
        :param coordinator: user who is doing the inviting
        :type coordinator: dict
        :param profile: Profile to apply to (applet ∩ user) if the invitation is
            accepted
        :type profile: dict or none
        :param idCode: ID code to apply to (applet ∩ user) if invitation is
            accepted
        :type idCode: string or None
        :returns: The invitation document that was created.
        """
        from .applet import Applet
        from .profile import Profile

        if not(Applet().isCoordinator(applet['_id'], coordinator)):
            raise AccessException(
                'You do not have adequate permissions to invite users to this '
                'applet ({}).'.format(Applet().preferredName(applet))
            )

        codified = (isinstance(idCode, str) and len(idCode))

        existing = self.findOne({
            'appletId': applet['_id'],
            'idCode': idCode
        }) if codified else None

        if existing:
            return existing

        now = datetime.datetime.utcnow()

        invitation = {
            'appletId': applet['_id'],
            'created': now,
            'updated': now,
            'role': role,
            'size': 0,
            'invitedBy': Profile().coordinatorProfile(
                applet['_id'],
                coordinator
            )
        }

        if codified:
            invitation["idCode"] = idCode

        if isinstance(profile, dict):
            invitation["coordinatorDefined"] = profile

        self.setPublic(invitation, False, save=False)

        # Now validate and save the profile.
        return ({
            k: v for k, v in self.save(invitation, validate=False).items() if (
                k!="idCode" and v is not None
            )
        })

    def acceptInvitation(self, invitation, user):
        from .applet import Applet
        from .ID_code import IDCode
        from .profile import Profile

        applet = Applet().load(invitation['appletId'], force=True)
        profiles = None
        if 'idCode' in invitation:
            profiles = IDCode().findProfile(invitation['idCode'])
        if profiles and len(profiles):
            profile = [
                pro for pro in profiles if str(
                    pro.get('userId')
                )==str(user['_id'])
            ]
            profile = profile[0] if len(profile) else None
        else:
            profile=None
        if profiles==None or profile==None or not len(profile):
            profile = Profile().createProfile(
                applet,
                user,
                role=invitation.get('role', 'user')
            )
            IDCode().createIdCode(profile, invitation.get('idCode'))
        if 'schema:knows' in invitation:
            if 'schema:knows' not in profile:
                profile['schema:knows'] = invitation['schema:knows']
            else:
                for k in invitation['schema:knows']:
                    if k in profile['schema:knows']:
                        profile['schema:knows'][k].extend([
                            r for r in invitation['schema:knows'][
                                k
                            ] if r not in profile['schema:knows'][k]
                        ])
                    else:
                        profile['schema:knows'][k] = invitation['schema:knows'][
                            k
                        ]
        Profile().save(profile, validate=False)
        self.remove(invitation)
        return(Profile().displayProfileFields(
            Profile().load(profile['_id'], force=True),
            user
        ))

    def htmlInvitation(
        self,
        invitation,
        invitee=None,
        fullDoc=False,
        includeLink=True
    ):
        """
        Returns an HTML document rendering the invitation.

        :param invitation: Invitation to render
        :type invitation: dict
        :param invitee: Invited user
        :type invitee: dict or None

        :returns: html document
        """
        from .applet import Applet
        from .profile import Profile
        from .protocol import Protocol
        from .token import Token
        from .user import User
        from girderformindlogger.exceptions import GirderException
        from girderformindlogger.api.rest import getApiUrl
        from girderformindlogger.utility import context as contextUtil,        \
            mail_utils

        accept = (
            "To accept or decline, visit <a href=\"{u}\">{u}</a>".format(
                u="https://web.mindlogger.org/#/invitation/{}".format(str(
                    invitation['_id']
                ))
            )
        ) if includeLink else ""
        applet = Applet().load(ObjectId(invitation['appletId']), force=True)
        appletName = Applet().preferredName(applet)
        try:
            skin = contextUtil.getSkin()
        except:
            skin = {}
        instanceName = skin.get("name", "MindLogger")
        role = invitation.get("role", "user")
        try:
            coordinator = Profile().coordinatorProfile(
                applet['_id'],
                invitation["invitedBy"]
            )
        except:
            coordinator = None
        displayProfile = Profile().displayProfileFields(invitation, invitee)
        description = applet.get('meta', {}).get(
            'applet',
            {}
        ).get(
            "schema:desciription",
            Protocol().load(
                applet['meta']['protocol']['_id'].split('protocol/')[-1],
                force=True
            ).get('meta', {}).get('protocol') if 'protocol' in applet.get(
                'meta',
                {}
            ) else {}
        ).get("schema:description", "")
        managers = mail_utils.htmlUserList(
            Applet().listUsers(applet, 'manager', force=True)
        )
        coordinators = mail_utils.htmlUserList(
            Applet().listUsers(applet, 'coordinator', force=True)
        )
        reviewers = mail_utils.htmlUserList(
            Applet().listUsers(applet, 'reviewer', force=True)
        )
        body = """
{greeting}ou have been invited {byCoordinator}to be {role} of <b>{appletName}</b>{instanceName}.
<br/>
{description}
{reviewers}
{managers}
{coordinators}
<br/>
{accept}
        """.format(
            accept=accept,
            appletName=appletName,
            byCoordinator="by {} ({}) ".format(
                coordinator.get("displayName", "an anonymous entity"),
                "<a href=\"mailto:{email}\">{email}</a>".format(
                    email=coordinator["email"]
                ) if "email" in coordinator else "email address unavailable"
            ) if isinstance(coordinator, dict) else "",
            coordinators="<h3>Who can change this applet's settings, "
                "but cannot see your data or enable others to see your data: </h3>{}"
                "".format(
                    coordinators if len(
                        coordinators
                    ) else "<ul><li>None</li></ul>"
                ),
            description="<h2>Description</h2><p>{}</p>".format(
                description
            ) if len(description) else "",
            greeting="Welcome {}; y".format(
                displayProfile['displayName']
            ) if 'displayName' in displayProfile else "Welcome! Y",
            instanceName=" on {}".format(
                instanceName
            ) if instanceName is not None and len(instanceName) else "",
            managers="<h3>Who can change this applet's settings, "
                "and can invite anyone to see your data: </h3>{}"
                "".format(
                    managers if len(managers) else "<ul><li>None</li></ul>"
                ),
            reviewers="<h3>Who can see your data for this applet: </h3>{}"
                "".format(
                    reviewers if len(reviewers) else "<ul><li>None</li></ul>"
                ),
            role="an editor" if role=="editor" else "a {}".format(role)
        ).strip()
        return(body if not fullDoc else """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Invitation to {appletName} on {instanceName}</title>
</head>
<body>
{body}
</body>
</html>
        """.format(
            appletName=appletName,
            instanceName=instanceName,
            body=body
        ).strip())

    def countFolders(self, folder, user=None, level=None):
        """
        Returns the number of subfolders within the given profile. Access
        checking is optional; to circumvent access checks, pass ``level=None``.

        :param folder: The parent profile.
        :type folder: dict
        :param user: If performing access checks, the user to check against.
        :type user: dict or None
        :param level: The required access level, or None to return the raw
            subfolder count.
        """
        fields = () if level is None else ('access', 'public')

        folders = self.findWithPermissions({
            'appletId': folder['_id'],
            'parentCollection': 'profile'
        }, fields=fields, user=user, level=level)

        return folders.count()

    def subtreeCount(self, folder, includeItems=True, user=None, level=None):
        """
        Return the size of the subtree rooted at the given profile. Includes
        the root profile in the count.

        :param folder: The root of the subtree.
        :type folder: dict
        :param includeItems: Whether to include items in the subtree count, or
            just folders.
        :type includeItems: bool
        :param user: If filtering by permission, the user to filter against.
        :param level: If filtering by permission, the required permission level.
        :type level: AccessLevel
        """
        count = 1

        if includeItems:
            count += self.countItems(folder)

        folders = self.findWithPermissions({
            'appletId': folder['_id'],
            'parentCollection': 'profile'
        }, fields='access', user=user, level=level)

        count += sum(self.subtreeCount(subfolder, includeItems=includeItems,
                                       user=user, level=level)
                     for subfolder in folders)

        return count

    def fileList(self, doc, user=None, path='', includeMetadata=False,
                 subpath=True, mimeFilter=None, data=True):
        """
        This function generates a list of 2-tuples whose first element is the
        relative path to the file from the profile's root and whose second
        element depends on the value of the `data` flag. If `data=True`, the
        second element will be a generator that will generate the bytes of the
        file data as stored in the assetstore. If `data=False`, the second
        element is the file document itself.

        :param doc: The folder to list.
        :param user: The user used for access.
        :param path: A path prefix to add to the results.
        :type path: str
        :param includeMetadata: if True and there is any metadata, include a
                                result which is the JSON string of the
                                metadata.  This is given a name of
                                metadata[-(number).json that is distinct from
                                any file within the folder.
        :type includeMetadata: bool
        :param subpath: if True, add the folder's name to the path.
        :type subpath: bool
        :param mimeFilter: Optional list of MIME types to filter by. Set to
            None to include all files.
        :type mimeFilter: `list or tuple`
        :param data: If True return raw content of each file as stored in the
            assetstore, otherwise return file document.
        :type data: bool
        :returns: Iterable over files in this folder, where each element is a
                  tuple of (path name of the file, stream function with file
                  data or file object).
        :rtype: generator(str, func)
        """
        from .item import Item

        itemModel = Item()
        if subpath:
            path = os.path.join(path, doc['name'])
        metadataFile = 'girder-folder-metadata.json'

        # Eagerly evaluate this list, as the MongoDB cursor can time out on long requests
        childFolders = list(self.childFolders(
            parentType='profile', parent=doc, user=user,
            fields=['name'] + (['meta'] if includeMetadata else [])
        ))
        for sub in childFolders:
            if sub['name'] == metadataFile:
                metadataFile = None
            for (filepath, file) in self.fileList(
                    sub, user, path, includeMetadata, subpath=True,
                    mimeFilter=mimeFilter, data=data):
                yield (filepath, file)

        # Eagerly evaluate this list, as the MongoDB cursor can time out on long requests
        childItems = list(self.childItems(
            folder=doc, fields=['name'] + (['meta'] if includeMetadata else [])
        ))
        for item in childItems:
            if item['name'] == metadataFile:
                metadataFile = None
            for (filepath, file) in itemModel.fileList(
                    item, user, path, includeMetadata, mimeFilter=mimeFilter, data=data):
                yield (filepath, file)

        if includeMetadata and metadataFile and doc.get('meta', {}):
            def stream():
                yield json.dumps(doc['meta'], default=str)
            yield (os.path.join(path, metadataFile), stream)

    def setAccessList(self, doc, access, save=False, recurse=False, user=None,
                      progress=noProgress, setPublic=None, publicFlags=None, force=False):
        """
        Overrides AccessControlledModel.setAccessList to add a recursive
        option. When `recurse=True`, this will set the access list on all
        subfolders to which the given user has ADMIN access level. Any
        subfolders that the given user does not have ADMIN access on will be
        skipped.

        :param doc: The folder to set access settings on.
        :type doc: girderformindlogger.models.folder
        :param access: The access control list.
        :type access: dict
        :param save: Whether the changes should be saved to the database.
        :type save: bool
        :param recurse: Whether this access list should be propagated to all
            subfolders underneath this folder.
        :type recurse: bool
        :param user: The current user (for recursive mode filtering).
        :param progress: Progress context to update.
        :type progress: :py:class:`girderformindlogger.utility.progress.ProgressContext`
        :param setPublic: Pass this if you wish to set the public flag on the
            resources being updated.
        :type setPublic: bool or None
        :param publicFlags: Pass this if you wish to set the public flag list on
            resources being updated.
        :type publicFlags: flag identifier str, or list/set/tuple of them, or None
        :param force: Set this to True to set the flags regardless of the passed in
            user's permissions.
        :type force: bool
        """
        progress.update(increment=1, message='Updating ' + doc['name'])
        if setPublic is not None:
            self.setPublic(doc, setPublic, save=False)

        if publicFlags is not None:
            doc = self.setPublicFlags(doc, publicFlags, user=user, save=False, force=force)

        doc = AccessControlledModel.setAccessList(
            self, doc, access, user=user, save=save, force=force)

        if recurse:
            subfolders = self.findWithPermissions({
                'appletId': doc['_id'],
                'parentCollection': 'profile'
            }, user=user, level=AccessType.ADMIN)

            for folder in subfolders:
                self.setAccessList(
                    folder, access, save=True, recurse=True, user=user,
                    progress=progress, setPublic=setPublic, publicFlags=publicFlags, force=force)

        return doc
