var notify_badge_class = 'live_notify_badge';
var notify_menu_class = 'live_notify_list';
var notify_api_url = '/inbox/notifications/api/unread_list/';
var notify_fetch_count = 5;
var notify_unread_url = '/inbox/notifications/unread/';
var notify_mark_all_unread_url = '/inbox/notifications/mark-all-as-read/';
var notify_refresh_period = 15000;
var consecutive_misfires = 0;
var registered_functions = [];

function fill_notification_list(data) {
    var menus = document.getElementsByClassName(notify_menu_class);
    if (menus) {
        var messages = data.unread_list
            .map(function (item) {
                var message = "";
                if (typeof item.actor !== "undefined") {
                    message = item.actor;
                }
                if (typeof item.verb !== "undefined") {
                    message = message + " " + item.verb;
                }
                if (typeof item.target !== "undefined") {
                    message = message + " " + item.target;
                }
                if (typeof item.timestamp !== "undefined") {
                    message = message + " " + item.timestamp;
                }
                return "<li>" + message + "</li>";
            })
            .join("");

        for (var i = 0; i < menus.length; i++) {
            menus[i].innerHTML = messages;
        }
    }
}

function fill_notification_badge(data) {
    var badges = document.getElementsByClassName(notify_badge_class);
    var staticUrl = $("#statiUrl").attr("data-url") || "/static/";
    var notification_sound = false;
    var previousUnreadCount = parseInt(localStorage.getItem('previousUnreadCount')) || 0;
    var notificationConfig = document.getElementById('notificationConfig');

    if (notificationConfig) {
        notification_sound = notificationConfig.dataset.notificationSound === 'true';
    }

    if (notification_sound) {
        if (previousUnreadCount !== null && previousUnreadCount < data.unread_count) {
            var audio = new Audio(staticUrl + "audio/notification-sound.wav");
            audio.play().catch(function () {});
        }
    }

    for (var i = 0; i < badges.length; i++) {
        badges[i].innerHTML = data.unread_count;
    }
    localStorage.setItem('previousUnreadCount', data.unread_count);
}

function register_notifier(func) {
    registered_functions.push(func);
}

function updateNotificationCount() {
    var unreadCount = $(".oh-navbar__notification-item .oh-navbar__notification-dot--unread").length;
    var $badge = $(".live_notify_badge");
    if ($badge.length) {
        $badge.text(unreadCount);
    }
}

function fetch_api_data() {
    if (registered_functions.length > 0) {
        var r = new XMLHttpRequest();
        r.addEventListener("readystatechange", function (event) {
            if (this.readyState === 4) {
                if (this.status === 200) {
                    consecutive_misfires = 0;
                    var data = JSON.parse(r.responseText);
                    for (var i = 0; i < registered_functions.length; i++) {
                        registered_functions[i](data);
                    }
                } else {
                    consecutive_misfires++;
                }
            }
        });
        r.open("GET", notify_api_url + "?max=" + notify_fetch_count, true);
        r.send();
    }
    if (consecutive_misfires < 10) {
        setTimeout(fetch_api_data, notify_refresh_period);
    } else {
        var badges = document.getElementsByClassName(notify_badge_class);
        if (badges) {
            for (var i = 0; i < badges.length; i++) {
                badges[i].innerHTML = "!";
                badges[i].title = "Connection lost!";
            }
        }
    }
}

$(document).ready(function () {
    window.notify_badge_class = 'live_notify_badge';
    window.notify_menu_class = 'live_notify_list';
    window.notify_api_url = '/inbox/notifications/api/unread_list/';
    window.notify_fetch_count = 5;
    window.notify_unread_url = '/inbox/notifications/unread/';
    window.notify_mark_all_unread_url = '/inbox/notifications/mark-all-as-read/';
    window.notify_refresh_period = 15000;

    if (typeof register_notifier === 'function') {
        register_notifier(fill_notification_list);
        register_notifier(fill_notification_badge);
    }

    $("#viewallnotification").on("click", function () {
        $("#showallnotificationbtn").toggle();
    });

    $("#notificationClose").on("click", function () {
        $("#allNotifications").removeClass("oh-activity-sidebar--show");
    });

    $(document).on("click", ".oh-navbar__notification-item", function (event) {
        event.preventDefault();
        var notificationLink = $(this);
        var notificationId = notificationLink.data("id");
        $.ajax({
            type: "post",
            url: "/mark-as-read-notification-json/",
            data: {
                csrfmiddlewaretoken: getCookie("csrftoken"),
                notification_id: notificationId,
            },
            success: function (response) {
                if (response.success || response.error) {
                    window.location.href = notificationLink.attr("href");
                } else {
                    window.location.href = notificationLink.attr("href");
                    console.error("Failed to mark notification as read");
                }
            },
            error: function (xhr, status, error) {
                window.location.href = notificationLink.attr("href");
                console.error("Error:", status, error);
            },
        });
    });
});

if (typeof window !== 'undefined') {
    window.notify_badge_class = window.notify_badge_class || 'live_notify_badge';
    window.notify_menu_class = window.notify_menu_class || 'live_notify_list';
    window.notify_api_url = window.notify_api_url || '/inbox/notifications/api/unread_list/';
    window.notify_fetch_count = window.notify_fetch_count || 5;
    window.notify_unread_url = window.notify_unread_url || '/inbox/notifications/unread/';
    window.notify_mark_all_unread_url = window.notify_mark_all_unread_url || '/inbox/notifications/mark-all-as-read/';
    window.notify_refresh_period = window.notify_refresh_period || 15000;
}

setTimeout(fetch_api_data, 1000);
