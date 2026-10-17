$(document).ready(function(){
    const canvas = document.getElementById('candidateChart');
    if (!canvas) {
        return;
    }

    let myChart;

    function candidateChart(dataSet, labels){
        const data = {
            labels: labels,
            datasets: [{
                data: dataSet.map(item => item.data),
                backgroundColor: ['#C6BEC4', '#FFF255', '#55C4FF', '#FF4646', '#2AFF0C']
            }]
        };

        const ctx = canvas.getContext('2d');
        myChart = new Chart(ctx, {
            type: 'pie',
            data: data,
            options: {
                onClick: handleClick // Attach onClick event handler
            }
        });
    }

    function handleClick(event, chartElements) {
        if (!chartElements.length) {
            return;
        }

        const statusUrls = [
            'not_sent',
            'sent',
            'accepted',
            'rejected',
            'joined',
        ];
        const status = statusUrls[chartElements[0].index];
        if (status) {
            window.location.href = `/recruitment/candidate-view?offer_letter_status=${status}`;
        }
    }

    $.ajax({
        url: "/recruitment/candidate-status",
        type: "GET",
        success: function(response){
            dataSet = response.dataSet;
            labels = response.labels;
            candidateChart(dataSet, labels);
        },
    });

});
