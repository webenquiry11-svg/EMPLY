$(document).ready(function () {
    let myChart1;

    function hiringChart(dataSet, labels) {
      const data = {
        labels,
        datasets: dataSet,
      };
      const canvas = document.getElementById("hiringChart");
      if (!canvas) {
        return;
      }

      myChart1 = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data,
      });
    }

    function joining(){
      const year = $("#year").val();
      if (!year || !document.getElementById("hiringChart")) {
        return;
      }

      $.ajax({
        url: "/recruitment/dashboard-hiring",
        type: "GET",
        data: {
          id : year
        },
        success: function (response) {
          hiringChart(response.dataSet, response.labels);
        },
      });
    }

    $("#year").on("change", function () {
      if (myChart1) {
        myChart1.destroy();
      }
      joining()
    });

    joining()

    $('#chart2').on("click", function () {
      if (!myChart1) {
        return;
      }
      let chartType = myChart1.config.type
      if (chartType === 'line') {
        chartType = 'bar';
      } else if(chartType==='bar') {
          chartType = 'line';
      }
      myChart1.config.type = chartType;
      myChart1.update();
    });

  });
