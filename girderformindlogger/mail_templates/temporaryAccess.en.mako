<%include file="_header.mako"/>

<div style="max-width: 440px;">
    <p>
        A temporary access token was requested on your behalf.  You can access the
        ${brandName} system at
        <a href="${url}">${url}</a>
        Once you access the system, you will have the option to update your password.
    </p>

    <p>
        If you did not initiate this temporary access request, you can ignore this
        email.  Temporary access is only available with the link provided and expires
        15 minutes after it was requested.
    </p>
</div>

<%include file="_footer.mako"/>
