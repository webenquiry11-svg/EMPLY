(() => {
    document.querySelectorAll("[data-payroll-notification-widget]").forEach((widget) => {
        const toggle = widget.querySelector("[data-payroll-notification-toggle]");
        const count = widget.querySelector("[data-payroll-notification-count]");
        const panel = widget.querySelector("[data-payroll-notification-panel]");
        const list = widget.querySelector("[data-payroll-notification-list]");
        if (!toggle || !count || !panel || !list) return;

        const readUrl = widget.dataset.readUrl;
        const feedUrl = widget.dataset.feedUrl;

        const render = (items) => {
            list.replaceChildren();
            if (!items.length) {
                const empty = document.createElement("li");
                empty.className = "payroll-notification-empty";
                empty.textContent = "No payroll notifications yet.";
                list.append(empty);
                return;
            }

            items.forEach((item) => {
                const row = document.createElement("li");
                row.className = `payroll-notification-item${item.unread ? " is-unread" : ""}`;
                const link = document.createElement("a");
                link.className = "payroll-notification-link";
                link.href = item.url || "#";
                link.dataset.notificationId = item.id;
                link.dataset.notificationUrl = item.url || "";
                const message = document.createElement("span");
                message.className = "payroll-notification-message";
                message.textContent = item.verb;
                const timestamp = document.createElement("time");
                timestamp.className = "payroll-notification-time";
                timestamp.dateTime = item.timestamp;
                timestamp.textContent = new Intl.DateTimeFormat(undefined, {
                    dateStyle: "medium",
                    timeStyle: "short",
                }).format(new Date(item.timestamp));
                link.append(message, timestamp);
                row.append(link);
                list.append(row);
            });
        };

        const loadNotifications = async () => {
            try {
                const response = await fetch(feedUrl, {
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                    credentials: "same-origin",
                });
                if (!response.ok) throw new Error(`Notification request failed (${response.status}).`);
                const data = await response.json();
                const unreadCount = Number(data.unread_count) || 0;
                count.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
                count.hidden = unreadCount === 0;
                toggle.setAttribute("aria-label", `Payroll notifications${unreadCount ? `, ${unreadCount} unread` : ""}`);
                render(data.notifications || []);
            } catch (error) {
                list.replaceChildren();
                const failure = document.createElement("li");
                failure.className = "payroll-notification-empty is-error";
                failure.textContent = "Unable to load payroll notifications. Try again.";
                list.append(failure);
                console.error(error);
            }
        };

        toggle.addEventListener("click", async () => {
            panel.hidden = !panel.hidden;
            toggle.setAttribute("aria-expanded", String(!panel.hidden));
            if (!panel.hidden) await loadNotifications();
        });

        list.addEventListener("click", async (event) => {
            const link = event.target.closest("[data-notification-id]");
            if (!link) return;
            event.preventDefault();
            const destination = link.dataset.notificationUrl;
            try {
                const response = await fetch(readUrl.replace("/0/", `/${link.dataset.notificationId}/`), {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": widget.dataset.csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });
                if (!response.ok) throw new Error(`Could not mark notification as read (${response.status}).`);
                await loadNotifications();
                if (destination) window.location.assign(destination);
            } catch (error) {
                console.error(error);
            }
        });

        document.addEventListener("click", (event) => {
            if (!widget.contains(event.target)) {
                panel.hidden = true;
                toggle.setAttribute("aria-expanded", "false");
            }
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !panel.hidden) {
                panel.hidden = true;
                toggle.setAttribute("aria-expanded", "false");
                toggle.focus();
            }
        });

        loadNotifications();
        window.setInterval(() => {
            if (!document.hidden) loadNotifications();
        }, 30000);
    });
})();
